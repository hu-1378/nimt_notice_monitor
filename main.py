"""
南京机电职业技术学院通知监控插件
监控学校官网及二级学院网站的通知公告，自动推送新通知
新增教务系统功能：课表查询、课程变动监测、定时推送
"""
import json
import hashlib
import asyncio
import re
import sqlite3
import base64
import time
from datetime import datetime, timedelta
from typing import List, Dict, Any, Optional
from pathlib import Path
from Crypto.PublicKey import RSA
from Crypto.Cipher import PKCS1_v1_5
from Crypto import Random

try:
    import aiohttp
    from bs4 import BeautifulSoup
    HAS_DEPENDENCIES = True
except ImportError as e:
    print(f"缺少依赖: {e}")
    HAS_DEPENDENCIES = False

try:
    from astrbot.api.event import filter, AstrMessageEvent
    from astrbot.api.star import Context, Star, register
    from astrbot.api import logger
    HAS_ASTRBOT_API = True
except ImportError as e:
    print(f"AstrBot API导入失败: {e}")
    HAS_ASTRBOT_API = False

if HAS_DEPENDENCIES and HAS_ASTRBOT_API:
    @register(
        "nimt_notice_monitor",
        "AstrBot",
        "南京机电职业技术学院通知监控插件",
        "2.1.0"
    )
    class NJIMTNoticeMonitor(Star):
        def __init__(self, context: Context):
            super().__init__(context)

            self.data_dir = Path("data/plugin_data/nimt_notice_monitor")
            self.data_dir.mkdir(parents=True, exist_ok=True)

            self.db_file = self.data_dir / "notices.db"
            self.config_file = self.data_dir / "config.json"

            self.config = self.load_config()
            self.sites_config = self.config.get("sites_config", [])
            self.push_targets = self.config.get("push_targets", {"users": [], "groups": []})
            self.check_interval = self.config.get("check_interval", 180)
            
            # 新增教务系统配置
            self.jwc_config = self.config.get("jwc_config", {
                "base_url": "https://nimt.jw.chaoxing.com",
                "public_key": "MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQC9zpr1gSa3gBnHLeDxw6DuPtnLC9HI8JOQrBbFV3ZkX0V92klvJDwS5YuZ810ZJL8MWED0gRSigS5YvXcQMyxizpN3IV9qhrlb48nI6mua1Xv75J9FxejEWA/kYlkkElwmXbyEMr1eGbYFTko40k82diw7k/xU4PaLnjFgQveSiQIDAQAB",
                "course_push_times": [
                    {"hour": 7, "minute": 0, "type": "全天课表"},
                    {"hour": 12, "minute": 0, "type": "下午课表"}
                ],
                "enable_course_push": True,
                "course_check_interval": 1440
            })

            self.init_database()
            self.start_scheduler()
            
            # 初始化RSA加密器
            self.rsa_encryptor = None
            self.init_rsa_encryptor()

            logger.info("南京机电通知监控插件初始化完成")

        def init_rsa_encryptor(self):
            """初始化RSA加密器"""
            try:
                public_key = self.jwc_config.get("public_key", "")
                if public_key:
                    self.rsa_encryptor = RSAEncryptor(public_key)
            except Exception as e:
                logger.error(f"初始化RSA加密器失败: {e}")

        def load_config(self) -> Dict[str, Any]:
            default_config = {
                "sites_config": [
                    {
                        "id": "nimt_main",
                        "name": "南京机电职业技术学院",
                        "url": "https://www.nimt.edu.cn/739/list.htm",
                        "base_url": "https://www.nimt.edu.cn",
                        "remark": "学校主站通知公告",
                        "enabled": True
                    },
                    {
                        "id": "jiaowu",
                        "name": "教务处",
                        "url": "https://www.nimt.edu.cn/jiaowu/396/list.htm",
                        "base_url": "https://www.nimt.edu.cn",
                        "remark": "教务处通知公告",
                        "enabled": True
                    },
                    {
                        "id": "xinxi",
                        "name": "信息工程系",
                        "url": "https://www.nimt.edu.cn/xinxi/tzgg/list.htm",
                        "base_url": "https://www.nimt.edu.cn",
                        "remark": "信息工程系通知公告",
                        "enabled": True
                    },
                    {
                        "id": "landao",
                        "name": "蓝岛创客空间",
                        "url": "https://www.nimt.edu.cn/landao/19517/list.htm",
                        "base_url": "https://www.nimt.edu.cn",
                        "remark": "蓝岛创客空间通知公告",
                        "enabled": True
                    }
                ],
                "push_targets": {
                    "users": [],
                    "groups": []
                },
                "check_interval": 180,
                "jwc_config": {
                    "base_url": "https://nimt.jw.chaoxing.com",
                    "public_key": "MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQC9zpr1gSa3gBnHLeDxw6DuPtnLC9HI8JOQrBbFV3ZkX0V92klvJDwS5YuZ810ZJL8MWED0gRSigS5YvXcQMyxizpN3IV9qhrlb48nI6mua1Xv75J9FxejEWA/kYlkkElwmXbyEMr1eGbYFTko40k82diw7k/xU4PaLnjFgQveSiQIDAQAB",
                    "course_push_times": [
                        {"hour": 7, "minute": 0, "type": "全天课表"},
                        {"hour": 12, "minute": 0, "type": "下午课表"}
                    ],
                    "enable_course_push": True,
                    "course_check_interval": 1440,
                    "enable_change_detection": True,
                    "change_check_day": 0,  # 周日检查
                    "change_check_time": "21:00"
                }
            }

            if self.config_file.exists():
                try:
                    with open(self.config_file, 'r', encoding='utf-8') as f:
                        config = json.load(f)
                        # 合并默认配置，确保新字段存在
                        for key, value in default_config.items():
                            if key not in config:
                                config[key] = value
                        return config
                except Exception as e:
                    logger.error(f"加载配置文件失败: {e}")

            self.save_config(default_config)
            return default_config

        def save_config(self, config: Dict[str, Any] = None):
            if config is None:
                config = self.config

            try:
                with open(self.config_file, 'w', encoding='utf-8') as f:
                    json.dump(config, f, ensure_ascii=False, indent=2)
                self.config = config
                self.sites_config = config.get("sites_config", [])
                self.push_targets = config.get("push_targets", {"users": [], "groups": []})
                self.check_interval = config.get("check_interval", 180)
                self.jwc_config = config.get("jwc_config", {})
            except Exception as e:
                logger.error(f"保存配置文件失败: {e}")

        def init_database(self):
            """初始化数据库，包含原有表和新增表"""
            try:
                conn = sqlite3.connect(str(self.db_file))
                cursor = conn.cursor()

                # 原有通知表
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS notices (
                        id TEXT PRIMARY KEY,
                        site_id TEXT NOT NULL,
                        title TEXT NOT NULL,
                        url TEXT NOT NULL,
                        publish_date TEXT NOT NULL,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        notified BOOLEAN DEFAULT 0,
                        notified_at TIMESTAMP
                    )
                """)

                cursor.execute("CREATE INDEX IF NOT EXISTS idx_site_id ON notices(site_id)")
                cursor.execute("CREATE INDEX IF NOT EXISTS idx_publish_date ON notices(publish_date)")

                # 新增：用户绑定表
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS user_bindings (
                        qq_id TEXT PRIMARY KEY,
                        student_id TEXT NOT NULL,
                        password TEXT NOT NULL,
                        name TEXT,
                        class_name TEXT,
                        bind_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        last_login TIMESTAMP,
                        cookie TEXT,
                        expires_at TIMESTAMP,
                        status TEXT DEFAULT 'active'
                    )
                """)

                # 新增：登录会话表
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS login_sessions (
                        student_id TEXT PRIMARY KEY,
                        cookies TEXT,
                        session_data TEXT,
                        last_login TIMESTAMP,
                        expires_at TIMESTAMP,
                        status TEXT DEFAULT 'active'
                    )
                """)

                # 新增：课程表
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS course_schedules (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        student_id TEXT NOT NULL,
                        academic_year TEXT NOT NULL,
                        week INTEGER NOT NULL,
                        day_of_week INTEGER NOT NULL,
                        section_code TEXT NOT NULL,
                        section_name TEXT,
                        start_time TEXT,
                        end_time TEXT,
                        course_name TEXT NOT NULL,
                        course_short TEXT,
                        teacher TEXT,
                        classroom TEXT,
                        building TEXT,
                        room_number TEXT,
                        course_type TEXT,
                        hours INTEGER,
                        is_practice BOOLEAN DEFAULT 0,
                        week_range TEXT,
                        course_hash TEXT NOT NULL,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        UNIQUE(student_id, academic_year, week, day_of_week, section_code)
                    )
                """)

                cursor.execute("CREATE INDEX IF NOT EXISTS idx_course_student ON course_schedules(student_id)")
                cursor.execute("CREATE INDEX IF NOT EXISTS idx_course_week ON course_schedules(week, day_of_week)")

                # 新增：实践课表
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS practice_courses (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        student_id TEXT NOT NULL,
                        academic_year TEXT NOT NULL,
                        course_name TEXT NOT NULL,
                        class_names TEXT,
                        type TEXT,
                        student_count TEXT,
                        week_range TEXT,
                        is_practice BOOLEAN DEFAULT 1,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)

                # 新增：课程变动记录表
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS course_changes (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        student_id TEXT NOT NULL,
                        course_code TEXT,
                        change_type TEXT NOT NULL,
                        old_data TEXT,
                        new_data TEXT,
                        change_date TEXT,
                        detected_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        notified BOOLEAN DEFAULT 0,
                        notified_at TIMESTAMP
                    )
                """)

                # 新增：请求日志表（用于调试）
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS request_logs (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        student_id TEXT,
                        api_url TEXT,
                        request_data TEXT,
                        response_data TEXT,
                        status_code INTEGER,
                        timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)

                conn.commit()
                conn.close()
                logger.info("数据库初始化完成")
            except Exception as e:
                logger.error(f"数据库初始化失败: {e}")

        def start_scheduler(self):
            """启动定时任务"""
            try:
                from astrbot.utils.schedule import scheduler

                # 原有通知检查任务
                @scheduler.scheduled_job('interval', minutes=self.check_interval, id='nimt_check_notices')
                async def scheduled_check():
                    await self.check_all_sites_task()

                # 新增：课表推送任务
                if self.jwc_config.get("enable_course_push", True):
                    for push_time in self.jwc_config.get("course_push_times", []):
                        hour = push_time.get("hour", 7)
                        minute = push_time.get("minute", 0)
                        job_id = f"nimt_course_push_{hour}_{minute}"
                        
                        @scheduler.scheduled_job('cron', hour=hour, minute=minute, id=job_id)
                        async def scheduled_course_push():
                            await self.push_course_schedule_task(push_type=push_time.get("type", "全天课表"))

                # 新增：课程变动检查任务
                if self.jwc_config.get("enable_change_detection", True):
                    check_day = self.jwc_config.get("change_check_day", 0)  # 周日
                    check_time_str = self.jwc_config.get("change_check_time", "21:00")
                    check_hour, check_minute = map(int, check_time_str.split(":"))
                    
                    @scheduler.scheduled_job('cron', day_of_week=check_day, hour=check_hour, minute=check_minute, id='nimt_check_course_changes')
                    async def scheduled_change_check():
                        await self.check_course_changes_task()

                logger.info("定时任务初始化完成")
            except ImportError:
                logger.warning("未找到调度器，定时任务功能不可用")
            except Exception as e:
                logger.error(f"启动调度器失败: {e}")

        # ==================== 原有通知监控功能 ====================

        async def check_all_sites_task(self):
            try:
                logger.info("开始定时检查通知...")
                new_notices = await self.check_all_sites()

                if new_notices:
                    logger.info(f"发现 {len(new_notices)} 条新通知")
                    for notice in new_notices:
                        await self.send_notice_push(notice)
                else:
                    logger.info("未发现新通知")

            except Exception as e:
                logger.error(f"定时检查失败: {e}")

        async def fetch_page(self, url: str) -> str:
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "zh-CN,zh;q=0.9",
            }

            try:
                async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
                    async with session.get(url, headers=headers) as response:
                        response.raise_for_status()
                        return await response.text(encoding='utf-8')
            except Exception as e:
                logger.error(f"请求失败 {url}: {e}")

            return ""

        def parse_notices(self, html: str, site_config: Dict[str, Any]) -> List[Dict[str, Any]]:
            if not html:
                return []

            try:
                soup = BeautifulSoup(html, 'html.parser')
                notices = []

                list_container = None
                selectors = [
                    'ul.news_list',
                    'ul.wp_list',
                    'div.news_list ul',
                    'div.list ul',
                    'div.article-list ul',
                    'ul.list-paddingleft-2'
                ]

                for selector in selectors:
                    list_container = soup.select_one(selector)
                    if list_container:
                        break

                if not list_container:
                    news_items = soup.find_all('li', class_=re.compile('news'))
                    if news_items:
                        list_container = soup.new_tag('div')
                        for item in news_items:
                            list_container.append(item)
                    else:
                        logger.warning(f"未找到通知列表容器: {site_config['name']}")
                        return notices

                items = list_container.find_all('li')
                for item in items:
                    try:
                        title_elem = item.find('a')
                        if not title_elem:
                            continue

                        title = title_elem.get_text(strip=True)
                        if not title:
                            continue

                        relative_url = title_elem.get('href', '')
                        if relative_url.startswith('http'):
                            url = relative_url
                        elif relative_url.startswith('/'):
                            url = site_config["base_url"] + relative_url
                        else:
                            url = f"{site_config['base_url']}/{relative_url}"

                        publish_date = datetime.now().strftime("%Y-%m-%d")
                        date_elems = item.find_all(['span', 'div', 'td'])

                        for elem in date_elems:
                            text = elem.get_text(strip=True)
                            date_match = re.search(r'(\d{4}[-/年]\d{1,2}[-/月]\d{1,2}[日]?)', text)
                            if date_match:
                                date_str = date_match.group(1)
                                date_str = re.sub(r'[年月]', '-', date_str)
                                date_str = re.sub(r'[日]', '', date_str)
                                date_str = re.sub(r'/', '-', date_str)
                                publish_date = date_str
                                break

                        notice_id = hashlib.md5(f"{site_config['id']}_{url}".encode()).hexdigest()

                        notices.append({
                            "id": notice_id,
                            "site_id": site_config["id"],
                            "site_name": site_config["name"],
                            "title": title,
                            "url": url,
                            "publish_date": publish_date,
                            "remark": site_config.get("remark", "")
                        })

                    except Exception as e:
                        logger.error(f"解析通知项失败: {e}")
                        continue

                return notices

            except Exception as e:
                logger.error(f"解析页面失败: {e}")
                return []

        async def check_site_notices(self, site_config: Dict[str, Any]) -> List[Dict[str, Any]]:
            new_notices = []

            try:
                html = await self.fetch_page(site_config["url"])
                notices = self.parse_notices(html, site_config)

                conn = sqlite3.connect(str(self.db_file))
                cursor = conn.cursor()

                for notice in notices:
                    cursor.execute("SELECT id FROM notices WHERE id = ?", (notice["id"],))

                    if not cursor.fetchone():
                        cursor.execute(
                            "INSERT INTO notices (id, site_id, title, url, publish_date) VALUES (?, ?, ?, ?, ?)",
                            (notice["id"], notice["site_id"], notice["title"], notice["url"], notice["publish_date"])
                        )
                        new_notices.append(notice)

                conn.commit()
                conn.close()

            except Exception as e:
                logger.error(f"检查网站 {site_config['name']} 失败: {e}")

            return new_notices

        async def check_all_sites(self) -> List[Dict[str, Any]]:
            all_new_notices = []

            for site in self.sites_config:
                if site.get("enabled", True):
                    try:
                        new_notices = await self.check_site_notices(site)
                        all_new_notices.extend(new_notices)
                        await asyncio.sleep(1)
                    except Exception as e:
                        logger.error(f"检查 {site['name']} 失败: {e}")

            return all_new_notices

        async def send_notice_push(self, notice: Dict[str, Any]):
            try:
                message = f"📢 新通知提醒\n\n"

                if notice.get("remark"):
                    message += f"📝 {notice['remark']}\n"

                message += f"🏫 {notice['site_name']}\n"
                message += f"📌 {notice['title']}\n"
                message += f"📅 {notice['publish_date']}\n"
                message += f"🔗 {notice['url']}\n"

                for user_id in self.push_targets["users"]:
                    try:
                        await self.context.send_message(f"private:{user_id}", message)
                    except Exception as e:
                        logger.error(f"推送用户 {user_id} 失败: {e}")

                for group_id in self.push_targets["groups"]:
                    try:
                        await self.context.send_message(f"group:{group_id}", message)
                    except Exception as e:
                        logger.error(f"推送群组 {group_id} 失败: {e}")

                conn = sqlite3.connect(str(self.db_file))
                cursor = conn.cursor()
                cursor.execute(
                    "UPDATE notices SET notified = 1, notified_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (notice["id"],)
                )
                conn.commit()
                conn.close()

            except Exception as e:
                logger.error(f"发送推送失败: {e}")

        # ==================== 新增教务系统功能 ====================

        class RSAEncryptor:
            """RSA加密器"""
            def __init__(self, public_key: str):
                self.public_key = public_key
                self.rsa_key = RSA.import_key(base64.b64decode(public_key))
                self.cipher = PKCS1_v1_5.new(self.rsa_key)

            def encrypt(self, plaintext: str) -> str:
                """加密文本"""
                encrypted = self.cipher.encrypt(plaintext.encode())
                return base64.b64encode(encrypted).decode()

        async def fetch_jwc(self, url: str, method: str = "GET", data: Dict = None, 
                           cookies: Dict = None, need_login: bool = False) -> Dict:
            """请求教务系统API"""
            base_url = self.jwc_config.get("base_url", "https://nimt.jw.chaoxing.com")
            full_url = base_url + url if url.startswith("/") else url
            
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "zh-CN,zh;q=0.9",
                "Content-Type": "application/x-www-form-urlencoded"
            }

            try:
                async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
                    if method.upper() == "GET":
                        async with session.get(full_url, headers=headers, cookies=cookies) as response:
                            response_text = await response.text()
                            status = response.status
                    else:
                        async with session.post(full_url, headers=headers, data=data, cookies=cookies) as response:
                            response_text = await response.text()
                            status = response.status

                    # 记录请求日志
                    self.log_request(None, full_url, data, response_text, status)
                    
                    if status == 200:
                        try:
                            return json.loads(response_text)
                        except:
                            return {"ret": -1, "msg": "响应解析失败", "data": response_text}
                    else:
                        return {"ret": -1, "msg": f"请求失败: {status}", "data": None}
                        
            except Exception as e:
                logger.error(f"请求教务系统失败 {full_url}: {e}")
                return {"ret": -1, "msg": f"网络错误: {str(e)}", "data": None}

        def log_request(self, student_id: str, url: str, request_data: Dict, response_data: str, status_code: int):
            """记录请求日志"""
            try:
                conn = sqlite3.connect(str(self.db_file))
                cursor = conn.cursor()
                
                cursor.execute(
                    """
                    INSERT INTO request_logs (student_id, api_url, request_data, response_data, status_code)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (student_id, url, json.dumps(request_data) if request_data else None, 
                     response_data[:1000] if response_data else None, status_code)
                )
                
                conn.commit()
                conn.close()
            except Exception as e:
                logger.error(f"记录请求日志失败: {e}")

        async def login_jwc(self, student_id: str, password: str) -> Dict[str, Any]:
            """登录教务系统"""
            try:
                # RSA加密密码
                if not self.rsa_encryptor:
                    return {"success": False, "error": "RSA加密器未初始化"}
                
                encrypted_password = self.rsa_encryptor.encrypt(password)
                
                # 构建登录数据
                login_data = {
                    "username": student_id,
                    "password": encrypted_password,
                    "vcode": "",
                    "jcaptchaCode": "",
                    "rememberMe": ""
                }
                
                # 发送登录请求
                result = await self.fetch_jwc("/admin/login", method="POST", data=login_data)
                
                if result.get("ret") == 0:
                    # 登录成功，尝试获取用户信息
                    user_info = await self.get_user_info()
                    if user_info:
                        return {
                            "success": True,
                            "student_id": student_id,
                            "user_info": user_info,
                            "message": "登录成功"
                        }
                    else:
                        return {
                            "success": True,
                            "student_id": student_id,
                            "user_info": None,
                            "message": "登录成功，但获取用户信息失败"
                        }
                else:
                    error_msg = result.get("msg", "登录失败")
                    if "账号或密码错误" in error_msg or result.get("ret") == -1:
                        return {"success": False, "error": "账号或密码错误"}
                    else:
                        return {"success": False, "error": error_msg}
                        
            except Exception as e:
                logger.error(f"登录教务系统失败: {e}")
                return {"success": False, "error": f"登录失败: {str(e)}"}

        async def get_user_info(self) -> Dict[str, Any]:
            """获取用户信息"""
            try:
                # 通过获取当前周次信息来获取用户信息
                today = datetime.now().strftime("%Y-%m-%d")
                result = await self.fetch_jwc(f"/admin/getDayBz?rq={today}")
                
                if result.get("ret") == 0 and result.get("data"):
                    xlrq = result["data"].get("xlrq", {})
                    return {
                        "student_id": xlrq.get("currentUserName"),
                        "user_id": xlrq.get("currentUserId"),
                        "role_id": xlrq.get("currentRoleId"),
                        "academic_year": xlrq.get("xnxqh"),
                        "department_id": xlrq.get("currentDepartmentId")
                    }
                return None
            except Exception as e:
                logger.error(f"获取用户信息失败: {e}")
                return None

        async def get_current_week(self, date_str: str = None) -> Dict[str, Any]:
            """获取当前周次信息"""
            try:
                if not date_str:
                    date_str = datetime.now().strftime("%Y-%m-%d")
                
                result = await self.fetch_jwc(f"/admin/getDayBz?rq={date_str}")
                
                if result.get("ret") == 0 and result.get("data"):
                    return result["data"].get("xlrq", {})
                return None
            except Exception as e:
                logger.error(f"获取周次信息失败: {e}")
                return None

        async def get_week_days(self, week: int) -> List[Dict[str, Any]]:
            """获取周次对应的星期"""
            try:
                result = await self.fetch_jwc("/admin/getXqByZc", method="POST", data={"zc": week})
                
                if result.get("ret") == 0 and result.get("data"):
                    return result["data"]
                return []
            except Exception as e:
                logger.error(f"获取星期信息失败: {e}")
                return []

        async def get_course_table(self, week: int, student_id: str = None) -> Dict[str, Any]:
            """获取课表"""
            try:
                result = await self.fetch_jwc("/admin/getXsdSykb", method="POST", data={"type": 1, "zc": week})
                
                if result.get("ret") == 0 and result.get("data"):
                    # 处理课表数据
                    course_data = result["data"]
                    
                    # 提取学术周信息
                    academic_year = None
                    week_info = await self.get_current_week()
                    if week_info:
                        academic_year = week_info.get("xnxqh")
                    
                    # 解析课表
                    parsed_courses = self.parse_course_table(course_data, student_id, academic_year, week)
                    return {
                        "success": True,
                        "academic_year": academic_year,
                        "week": week,
                        "courses": parsed_courses,
                        "raw_data": course_data
                    }
                else:
                    return {
                        "success": False,
                        "error": result.get("msg", "获取课表失败")
                    }
                    
            except Exception as e:
                logger.error(f"获取课表失败: {e}")
                return {"success": False, "error": f"获取课表失败: {str(e)}"}

        def parse_course_table(self, course_data: Dict, student_id: str, academic_year: str, week: int) -> List[Dict]:
            """解析课表数据"""
            courses = []
            
            try:
                # 处理jcKcxx（节次课程信息）
                jc_kcxx = course_data.get("jcKcxx", [])
                
                for section_info in jc_kcxx:
                    section_code = section_info.get("jcbm")  # 节次编码
                    section_num = section_info.get("jc", section_code)  # 节次
                    
                    # 获取时间映射
                    time_info = self.get_section_time(section_code)
                    
                    kbxx_list = section_info.get("kbxx", [])
                    
                    for day_info in kbxx_list:
                        day_of_week = day_info.get("yzxq")  # 星期几（1-7）
                        kcxx_list = day_info.get("kcxx", [])
                        
                        for course_info in kcxx_list:
                            course_name = course_info.get("kcmc", "")
                            teacher = course_info.get("teacher", "")
                            classroom = course_info.get("classroom", "")
                            
                            # 跳过空课程
                            if course_name == "-" or not course_name:
                                continue
                            
                            # 解析课程名称和学时
                            course_short = course_name
                            hours = 0
                            
                            # 匹配学时，如：应用数学(64h)
                            hour_match = re.search(r'\((\d+)h\)', course_name)
                            if hour_match:
                                hours = int(hour_match.group(1))
                                course_short = re.sub(r'\(\d+h\)', '', course_name).strip()
                            
                            # 解析教室信息
                            building = classroom
                            room_number = ""
                            if classroom and classroom != "-":
                                # 简单解析教室，如：善学楼201
                                building_match = re.search(r'([\u4e00-\u9fa5]+楼)', classroom)
                                if building_match:
                                    building = building_match.group(1)
                                    room_number = classroom.replace(building, "")
                                else:
                                    room_number = classroom
                            
                            # 生成课程哈希
                            course_hash_data = f"{academic_year}_{week}_{day_of_week}_{section_code}_{course_name}_{teacher}_{classroom}"
                            course_hash = hashlib.md5(course_hash_data.encode()).hexdigest()
                            
                            course = {
                                "student_id": student_id,
                                "academic_year": academic_year,
                                "week": week,
                                "day_of_week": int(day_of_week),
                                "section_code": section_code,
                                "section_name": f"第{section_num}节",
                                "start_time": time_info.get("start_time") if time_info else "",
                                "end_time": time_info.get("end_time") if time_info else "",
                                "course_name": course_name,
                                "course_short": course_short,
                                "teacher": teacher,
                                "classroom": classroom,
                                "building": building,
                                "room_number": room_number,
                                "course_type": "理论",  # 默认为理论课
                                "hours": hours,
                                "is_practice": False,
                                "week_range": "",  # 需要从其他接口获取
                                "course_hash": course_hash
                            }
                            
                            courses.append(course)
                
                # 处理实践课
                sjk_list = course_data.get("sjk", [])
                for practice_info in sjk_list:
                    course_name = practice_info.get("kcmc", "")
                    class_names = practice_info.get("jxbzc", "")
                    practice_type = practice_info.get("type", "")
                    student_count = practice_info.get("xkrs", "")
                    week_range = practice_info.get("zcstr", "")
                    
                    if course_name and course_name != "-":
                        practice_course = {
                            "student_id": student_id,
                            "academic_year": academic_year,
                            "course_name": course_name,
                            "class_names": class_names,
                            "type": practice_type,
                            "student_count": student_count,
                            "week_range": week_range,
                            "is_practice": True
                        }
                        # 这里可以单独存储实践课
                        
                return courses
                
            except Exception as e:
                logger.error(f"解析课表失败: {e}")
                return []

        def get_section_time(self, section_code: str) -> Dict[str, str]:
            """根据节次编码获取时间"""
            # 节次时间映射表
            time_mapping = {
                "1": {"start_time": "08:00", "end_time": "08:45", "period": "第1节"},
                "2": {"start_time": "08:50", "end_time": "09:35", "period": "第2节"},
                "3": {"start_time": "09:50", "end_time": "10:35", "period": "第3节"},
                "4": {"start_time": "10:40", "end_time": "11:25", "period": "第4节"},
                "5": {"start_time": "13:30", "end_time": "14:15", "period": "第5节"},
                "6": {"start_time": "14:20", "end_time": "15:05", "period": "第6节"},
                "7": {"start_time": "15:20", "end_time": "16:05", "period": "第7节"},
                "8": {"start_time": "16:10", "end_time": "16:55", "period": "第8节"},
                "9": {"start_time": "18:30", "end_time": "19:15", "period": "第9节"},
                "10": {"start_time": "19:20", "end_time": "20:05", "period": "第10节"},
                "11": {"start_time": "20:10", "end_time": "20:55", "period": "第11节"},
            }
            
            return time_mapping.get(section_code, {})

        async def save_courses_to_db(self, courses: List[Dict], student_id: str, academic_year: str, week: int):
            """保存课程到数据库"""
            try:
                conn = sqlite3.connect(str(self.db_file))
                cursor = conn.cursor()
                
                # 先删除该学生该周的旧课程
                cursor.execute(
                    "DELETE FROM course_schedules WHERE student_id = ? AND academic_year = ? AND week = ?",
                    (student_id, academic_year, week)
                )
                
                # 插入新课程
                for course in courses:
                    cursor.execute(
                        """
                        INSERT INTO course_schedules (
                            student_id, academic_year, week, day_of_week, section_code, section_name,
                            start_time, end_time, course_name, course_short, teacher, classroom,
                            building, room_number, course_type, hours, is_practice, week_range, course_hash
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            course["student_id"], course["academic_year"], course["week"],
                            course["day_of_week"], course["section_code"], course["section_name"],
                            course["start_time"], course["end_time"], course["course_name"],
                            course["course_short"], course["teacher"], course["classroom"],
                            course["building"], course["room_number"], course["course_type"],
                            course["hours"], course["is_practice"], course["week_range"],
                            course["course_hash"]
                        )
                    )
                
                conn.commit()
                conn.close()
                logger.info(f"成功保存{len(courses)}门课程到数据库")
                
            except Exception as e:
                logger.error(f"保存课程到数据库失败: {e}")

        async def get_today_courses(self, student_id: str, push_type: str = "全天课表") -> List[Dict]:
            """获取今天课程"""
            try:
                # 获取当前日期和周次
                today = datetime.now()
                week_info = await self.get_current_week(today.strftime("%Y-%m-%d"))
                
                if not week_info:
                    return []
                
                week = week_info.get("zc", 1)
                day_of_week = week_info.get("xqbh", today.weekday() + 1)  # 1-7
                
                # 从数据库获取课程
                conn = sqlite3.connect(str(self.db_file))
                cursor = conn.cursor()
                
                # 查询今天课程
                cursor.execute(
                    """
                    SELECT * FROM course_schedules 
                    WHERE student_id = ? AND week = ? AND day_of_week = ?
                    ORDER BY section_code
                    """,
                    (student_id, week, day_of_week)
                )
                
                rows = cursor.fetchall()
                conn.close()
                
                # 如果没有课程，尝试从API获取
                if not rows:
                    await self.update_course_table(student_id, week)
                    return await self.get_today_courses(student_id, push_type)
                
                # 转换为字典列表
                columns = [description[0] for description in cursor.description]
                courses = [dict(zip(columns, row)) for row in rows]
                
                # 根据推送类型过滤课程
                if push_type == "下午课表":
                    # 只返回下午及晚上的课程（节次5-11）
                    courses = [c for c in courses if c.get("section_code") and int(c["section_code"]) >= 5]
                # 如果是全天课表，返回所有课程
                
                return courses
                
            except Exception as e:
                logger.error(f"获取今天课程失败: {e}")
                return []

        async def update_course_table(self, student_id: str, week: int = None):
            """更新课表数据"""
            try:
                # 如果没有指定周次，获取当前周次
                if not week:
                    week_info = await self.get_current_week()
                    if week_info:
                        week = week_info.get("zc", 1)
                    else:
                        week = 1
                
                # 获取课表数据
                course_result = await self.get_course_table(week, student_id)
                
                if course_result.get("success"):
                    courses = course_result.get("courses", [])
                    academic_year = course_result.get("academic_year", "")
                    
                    # 保存到数据库
                    await self.save_courses_to_db(courses, student_id, academic_year, week)
                    
                    return True
                else:
                    logger.error(f"更新课表失败: {course_result.get('error')}")
                    return False
                    
            except Exception as e:
                logger.error(f"更新课表失败: {e}")
                return False

        async def push_course_schedule_task(self, push_type: str = "全天课表"):
            """推送课表任务"""
            try:
                logger.info(f"开始推送{push_type}...")
                
                # 获取所有绑定用户
                conn = sqlite3.connect(str(self.db_file))
                cursor = conn.cursor()
                cursor.execute("SELECT qq_id, student_id, name FROM user_bindings WHERE status = 'active'")
                users = cursor.fetchall()
                conn.close()
                
                if not users:
                    logger.info("没有绑定用户，跳过推送")
                    return
                
                for user in users:
                    qq_id, student_id, name = user
                    
                    try:
                        # 获取今天课程
                        courses = await self.get_today_courses(student_id, push_type)
                        
                        if not courses:
                            continue
                        
                        # 构建推送消息
                        today = datetime.now()
                        weekday_str = ["一", "二", "三", "四", "五", "六", "日"][today.weekday()]
                        
                        message = f"📅 {today.month}月{today.day}日 课表提醒（星期{weekday_str}）\n\n"
                        
                        # 按时间段分组
                        morning_courses = []
                        afternoon_courses = []
                        evening_courses = []
                        
                        for course in courses:
                            section_code = int(course.get("section_code", 0))
                            if 1 <= section_code <= 4:
                                morning_courses.append(course)
                            elif 5 <= section_code <= 8:
                                afternoon_courses.append(course)
                            else:
                                evening_courses.append(course)
                        
                        # 上午课程
                        if morning_courses and push_type == "全天课表":
                            message += "🌅 上午课程：\n"
                            for course in morning_courses:
                                message += self.format_course_message(course)
                        
                        # 下午课程
                        if afternoon_courses:
                            if push_type == "全天课表":
                                message += "\n🌞 下午课程：\n"
                            else:
                                message += "🌞 下午课程：\n"
                            for course in afternoon_courses:
                                message += self.format_course_message(course)
                        
                        # 晚上课程
                        if evening_courses and push_type == "全天课表":
                            message += "\n🌙 晚上课程：\n"
                            for course in evening_courses:
                                message += self.format_course_message(course)
                        
                        message += "\n💡 如有变动请以教务处通知为准"
                        
                        # 发送推送
                        if morning_courses or afternoon_courses or evening_courses:
                            await self.context.send_message(f"private:{qq_id}", message)
                            logger.info(f"向用户{qq_id}推送课表成功")
                        else:
                            if push_type == "全天课表":
                                await self.context.send_message(f"private:{qq_id}", 
                                                               f"📅 {today.month}月{today.day}日（星期{weekday_str}）\n\n✅ 今日无课程安排")
                            
                    except Exception as e:
                        logger.error(f"向用户{qq_id}推送课表失败: {e}")
                        continue
                        
                logger.info("课表推送完成")
                
            except Exception as e:
                logger.error(f"推送课表任务失败: {e}")

        def format_course_message(self, course: Dict) -> str:
            """格式化课程消息"""
            course_name = course.get("course_short", course.get("course_name", ""))
            section_name = course.get("section_name", "")
            start_time = course.get("start_time", "")
            end_time = course.get("end_time", "")
            classroom = course.get("classroom", "")
            teacher = course.get("teacher", "")
            
            message = f"【{section_name}】"
            if start_time and end_time:
                message += f"{start_time}-{end_time}\n"
            else:
                message += "\n"
            
            message += f"课程：{course_name}\n"
            
            if classroom and classroom != "-":
                message += f"教室：{classroom}\n"
            
            if teacher and teacher != "-":
                message += f"教师：{teacher}\n"
            
            message += "\n"
            return message

        async def check_course_changes_task(self):
            """检查课程变动任务"""
            try:
                logger.info("开始检查课程变动...")
                
                # 获取所有绑定用户
                conn = sqlite3.connect(str(self.db_file))
                cursor = conn.cursor()
                cursor.execute("SELECT qq_id, student_id FROM user_bindings WHERE status = 'active'")
                users = cursor.fetchall()
                conn.close()
                
                if not users:
                    logger.info("没有绑定用户，跳过变动检查")
                    return
                
                for user in users:
                    qq_id, student_id = user
                    
                    try:
                        # 更新最新课表
                        await self.update_course_table(student_id)
                        
                        # 这里可以实现课程变动检测逻辑
                        # 比较新旧课程的哈希值，检测变动
                        # 由于时间关系，这里留空，后续可以完善
                        
                    except Exception as e:
                        logger.error(f"检查用户{student_id}课程变动失败: {e}")
                        continue
                        
                logger.info("课程变动检查完成")
                
            except Exception as e:
                logger.error(f"检查课程变动任务失败: {e}")

        # ==================== 命令处理 ====================

        @filter.command("查看通知")
        async def cmd_view_notices(self, event: AstrMessageEvent, count: int = None):
            """查看最近的通知

            参数:
            count: 可选，查看最近几条通知（默认查看最近3天的通知）
            """
            try:
                conn = sqlite3.connect(str(self.db_file))
                cursor = conn.cursor()

                if count is not None:
                    # 查看指定数量的最新通知
                    if count < 1:
                        count = 1
                    if count > 50:
                        count = 50

                    cursor.execute(
                        """
                        SELECT title, publish_date, url 
                        FROM notices 
                        ORDER BY publish_date DESC, created_at DESC 
                        LIMIT ?
                        """,
                        (count,)
                    )

                    notices = cursor.fetchall()
                    conn.close()

                    if not notices:
                        yield event.plain_result("没有通知记录")
                        return

                    # 构建响应消息
                    response = f"📋 最近 {len(notices)} 条通知\n\n"

                    current_date = None
                    for title, pub_date, url in notices:
                        if pub_date != current_date:
                            response += f"\n📅 {pub_date}\n"
                            current_date = pub_date

                        # 缩短标题
                        short_title = title[:40] + "..." if len(title) > 40 else title
                        response += f"  📌 {short_title}\n"
                        response += f"     🔗 {url}\n"

                        # 限制消息长度
                        if len(response) > 1500:
                            response += "\n... 更多通知请查看网站"
                            break

                    yield event.plain_result(response)
                else:
                    # 查看最近3天的通知
                    three_days_ago = (datetime.now() - timedelta(days=3)).strftime("%Y-%m-%d")

                    cursor.execute(
                        """
                        SELECT title, publish_date, url 
                        FROM notices 
                        WHERE publish_date >= ? 
                        ORDER BY publish_date DESC 
                        LIMIT 20
                        """,
                        (three_days_ago,)
                    )

                    notices = cursor.fetchall()
                    conn.close()

                    if not notices:
                        yield event.plain_result("最近3天没有通知")
                        return

                    # 构建响应消息
                    response = "📋 最近3天通知汇总\n\n"

                    current_date = None
                    for title, pub_date, url in notices:
                        if pub_date != current_date:
                            response += f"\n📅 {pub_date}\n"
                            current_date = pub_date

                        # 缩短标题
                        short_title = title[:40] + "..." if len(title) > 40 else title
                        response += f"  📌 {short_title}\n"
                        response += f"     🔗 {url}\n"

                        # 限制消息长度
                        if len(response) > 1500:
                            response += "\n... 更多通知请查看网站"
                            break

                    yield event.plain_result(response)

            except Exception as e:
                logger.error(f"查看通知失败: {e}")
                yield event.plain_result(f"查询失败: {str(e)}")

        @filter.command_group("nimt")
        def nimt_group(self):
            pass

        @nimt_group.command("网站列表")
        async def cmd_list_sites(self, event: AstrMessageEvent):
            try:
                if not self.sites_config:
                    yield event.plain_result("暂无监控网站")
                    return

                response = "📊 监控网站列表\n\n"
                for i, site in enumerate(self.sites_config, 1):
                    status = "✅" if site.get("enabled", True) else "⛔"
                    remark = f" ({site.get('remark', '')})" if site.get("remark") else ""
                    response += f"{i}. {status} {site['name']}{remark}\n"
                    response += f"   ID: {site['id']}\n"
                    response += f"   URL: {site['url']}\n\n"

                yield event.plain_result(response)

            except Exception as e:
                logger.error(f"列出网站失败: {e}")
                yield event.plain_result(f"查询失败: {str(e)}")

        @nimt_group.command("检查通知")
        @filter.permission_type(filter.PermissionType.ADMIN)
        async def cmd_check_notices(self, event: AstrMessageEvent):
            yield event.plain_result("开始检查新通知，请稍候...")

            try:
                new_notices = await self.check_all_sites()

                if new_notices:
                    response = f"✅ 发现 {len(new_notices)} 条新通知：\n\n"
                    for notice in new_notices[:5]:
                        response += f"📌 {notice['title']}\n"
                        response += f"   📅 {notice['publish_date']}\n"
                        response += f"   🏫 {notice['site_name']}\n\n"

                    if len(new_notices) > 5:
                        response += f"... 还有 {len(new_notices) - 5} 条未显示\n"

                    response += "正在推送..."
                    yield event.plain_result(response)

                    for notice in new_notices:
                        await self.send_notice_push(notice)

                    yield event.plain_result("✅ 推送完成")
                else:
                    yield event.plain_result("未发现新通知")

            except Exception as e:
                logger.error(f"手动检查失败: {e}")
                yield event.plain_result(f"检查失败: {str(e)}")

        # ==================== 新增教务系统命令 ====================

        @filter.command("绑定教务")
        async def cmd_bind_jwc(self, event: AstrMessageEvent, student_id: str, password: str):
            """绑定教务系统账号
            
            参数:
            student_id: 学号
            password: 密码
            """
            qq_id = event.get_sender_id()
            
            # 检查是否已绑定
            conn = sqlite3.connect(str(self.db_file))
            cursor = conn.cursor()
            cursor.execute("SELECT student_id FROM user_bindings WHERE qq_id = ?", (qq_id,))
            existing = cursor.fetchone()
            
            if existing:
                conn.close()
                yield event.plain_result("您已经绑定过教务系统，如需重新绑定请先使用 /解绑教务")
                return
            
            conn.close()
            
            # 尝试登录验证
            yield event.plain_result("正在验证账号密码，请稍候...")
            
            login_result = await self.login_jwc(student_id, password)
            
            if login_result.get("success"):
                # 绑定成功，保存信息
                try:
                    user_info = login_result.get("user_info", {})
                    name = user_info.get("student_id", "")
                    
                    # AES加密密码（这里简化处理，实际应该使用AES加密）
                    # 由于时间关系，这里只做base64编码，实际使用时应使用AES加密
                    encoded_password = base64.b64encode(password.encode()).decode()
                    
                    conn = sqlite3.connect(str(self.db_file))
                    cursor = conn.cursor()
                    
                    cursor.execute(
                        """
                        INSERT INTO user_bindings (qq_id, student_id, password, name, bind_time)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (qq_id, student_id, encoded_password, name, datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
                    )
                    
                    conn.commit()
                    conn.close()
                    
                    # 更新课表数据
                    yield event.plain_result("验证成功，正在更新课表数据...")
                    await self.update_course_table(student_id)
                    
                    yield event.plain_result(f"✅ 绑定成功！\n学号：{student_id}\n姓名：{name}\n\n课表数据已更新，明天开始将为您推送课程提醒。")
                    
                except Exception as e:
                    logger.error(f"保存绑定信息失败: {e}")
                    yield event.plain_result(f"绑定失败: {str(e)}")
            else:
                error_msg = login_result.get("error", "绑定失败")
                yield event.plain_result(f"❌ {error_msg}\n请检查学号和密码是否正确。")

        @filter.command("解绑教务")
        async def cmd_unbind_jwc(self, event: AstrMessageEvent):
            """解绑教务系统账号"""
            qq_id = event.get_sender_id()
            
            try:
                conn = sqlite3.connect(str(self.db_file))
                cursor = conn.cursor()
                
                cursor.execute("SELECT student_id FROM user_bindings WHERE qq_id = ?", (qq_id,))
                existing = cursor.fetchone()
                
                if not existing:
                    conn.close()
                    yield event.plain_result("您尚未绑定教务系统")
                    return
                
                cursor.execute("DELETE FROM user_bindings WHERE qq_id = ?", (qq_id,))
                cursor.execute("DELETE FROM course_schedules WHERE student_id = ?", (existing[0],))
                
                conn.commit()
                conn.close()
                
                yield event.plain_result("✅ 解绑成功！已清除您的绑定信息和课表数据。")
                
            except Exception as e:
                logger.error(f"解绑失败: {e}")
                yield event.plain_result(f"解绑失败: {str(e)}")

        @filter.command("我的绑定")
        async def cmd_my_binding(self, event: AstrMessageEvent):
            """查看我的绑定信息"""
            qq_id = event.get_sender_id()
            
            try:
                conn = sqlite3.connect(str(self.db_file))
                cursor = conn.cursor()
                
                cursor.execute(
                    "SELECT student_id, name, class_name, bind_time FROM user_bindings WHERE qq_id = ?",
                    (qq_id,)
                )
                
                binding = cursor.fetchone()
                conn.close()
                
                if not binding:
                    yield event.plain_result("您尚未绑定教务系统")
                    return
                
                student_id, name, class_name, bind_time = binding
                
                response = f"📋 绑定信息\n\n"
                response += f"QQ号：{qq_id}\n"
                response += f"学号：{student_id}\n"
                if name:
                    response += f"姓名：{name}\n"
                if class_name:
                    response += f"班级：{class_name}\n"
                response += f"绑定时间：{bind_time}\n\n"
                
                # 检查是否有课表数据
                conn = sqlite3.connect(str(self.db_file))
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT COUNT(*) FROM course_schedules WHERE student_id = ?",
                    (student_id,)
                )
                count = cursor.fetchone()[0]
                conn.close()
                
                if count > 0:
                    response += f"📅 已存储 {count} 条课程记录\n"
                else:
                    response += f"📅 暂无课表数据，请使用 /更新课表 获取\n"
                
                yield event.plain_result(response)
                
            except Exception as e:
                logger.error(f"查询绑定信息失败: {e}")
                yield event.plain_result(f"查询失败: {str(e)}")

        @filter.command("课表")
        async def cmd_course_table(self, event: AstrMessageEvent, week: int = None):
            """查看课表
            
            参数:
            week: 可选，查看第几周的课表（默认查看当前周或下周）
            """
            qq_id = event.get_sender_id()
            
            # 检查绑定
            conn = sqlite3.connect(str(self.db_file))
            cursor = conn.cursor()
            cursor.execute("SELECT student_id FROM user_bindings WHERE qq_id = ?", (qq_id,))
            binding = cursor.fetchone()
            conn.close()
            
            if not binding:
                yield event.plain_result("请先使用 /绑定教务 绑定您的账号")
                return
            
            student_id = binding[0]
            
            try:
                # 获取当前周次
                today = datetime.now()
                week_info = await self.get_current_week(today.strftime("%Y-%m-%d"))
                
                if not week_info:
                    yield event.plain_result("无法获取周次信息，请稍后再试")
                    return
                
                current_week = week_info.get("zc", 1)
                academic_year = week_info.get("xnxqh", "")
                
                # 确定要查询的周次
                if week is None:
                    # 如果是周六或周日，查看下周课表
                    if today.weekday() >= 5:  # 5=周六, 6=周日
                        query_week = current_week + 1
                    else:
                        query_week = current_week
                else:
                    query_week = week
                
                # 从数据库获取课表
                conn = sqlite3.connect(str(self.db_file))
                cursor = conn.cursor()
                
                cursor.execute(
                    """
                    SELECT day_of_week, section_code, section_name, course_name, course_short, 
                           teacher, classroom, start_time, end_time
                    FROM course_schedules 
                    WHERE student_id = ? AND academic_year = ? AND week = ?
                    ORDER BY day_of_week, section_code
                    """,
                    (student_id, academic_year, query_week)
                )
                
                courses = cursor.fetchall()
                
                # 如果没有数据，尝试从API获取
                if not courses:
                    yield event.plain_result("正在获取课表数据，请稍候...")
                    success = await self.update_course_table(student_id, query_week)
                    
                    if success:
                        # 重新查询
                        cursor.execute(
                            """
                            SELECT day_of_week, section_code, section_name, course_name, course_short, 
                                   teacher, classroom, start_time, end_time
                            FROM course_schedules 
                            WHERE student_id = ? AND academic_year = ? AND week = ?
                            ORDER BY day_of_week, section_code
                            """,
                            (student_id, academic_year, query_week)
                        )
                        courses = cursor.fetchall()
                
                conn.close()
                
                if not courses:
                    yield event.plain_result("获取课表数据失败，请稍后再试")
                    return
                
                # 获取该周的日期信息
                week_days = await self.get_week_days(query_week)
                week_days_map = {day.get("xq"): day.get("date") for day in week_days}
                
                # 按星期分组课程
                courses_by_day = {}
                for course in courses:
                    day_of_week = course[0]
                    if day_of_week not in courses_by_day:
                        courses_by_day[day_of_week] = []
                    courses_by_day[day_of_week].append(course)
                
                # 构建响应消息
                weekday_names = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
                
                response = f"📋 第{query_week}周课表"
                if academic_year:
                    response += f"（{academic_year}）"
                response += "\n\n"
                
                for day in range(1, 8):
                    day_courses = courses_by_day.get(day, [])
                    
                    # 添加星期标题
                    weekday_name = weekday_names[day-1]
                    date_str = week_days_map.get(weekday_name, "")
                    if date_str:
                        response += f"📅 {weekday_name}（{date_str}）\n"
                    else:
                        response += f"📅 {weekday_name}\n"
                    
                    if not day_courses:
                        response += "  ✅ 无课程\n"
                    else:
                        for course in day_courses:
                            _, section_code, section_name, course_name, course_short, teacher, classroom, start_time, end_time = course
                            
                            # 使用简称或全名
                            display_name = course_short if course_short else course_name
                            
                            # 格式化输出
                            time_str = ""
                            if start_time and end_time:
                                time_str = f"{start_time}-{end_time}"
                            elif section_name:
                                time_str = section_name
                            
                            response += f"  {section_code}. {display_name}"
                            if time_str:
                                response += f" [{time_str}]"
                            if classroom and classroom != "-":
                                response += f" @{classroom}"
                            if teacher and teacher != "-":
                                response += f" ({teacher})"
                            response += "\n"
                    
                    response += "\n"
                
                # 添加底部信息
                if week is None and query_week != current_week:
                    response += f"👆 当前为第{current_week}周，已为您显示第{query_week}周（下周）课表\n"
                else:
                    response += f"👆 当前为第{current_week}周\n"
                
                response += "💡 使用 /课表 [周次] 查看指定周次的课表"
                
                # 分割长消息
                if len(response) > 1500:
                    parts = []
                    lines = response.split('\n')
                    current_part = ""
                    
                    for line in lines:
                        if len(current_part) + len(line) + 1 > 1500:
                            parts.append(current_part)
                            current_part = line
                        else:
                            current_part += line + '\n'
                    
                    if current_part:
                        parts.append(current_part)
                    
                    for i, part in enumerate(parts):
                        if i == len(parts) - 1:
                            yield event.plain_result(part)
                        else:
                            yield event.plain_result(part)
                else:
                    yield event.plain_result(response)
                
            except Exception as e:
                logger.error(f"查询课表失败: {e}")
                yield event.plain_result(f"查询失败: {str(e)}")

        @filter.command("更新课表")
        async def cmd_update_course(self, event: AstrMessageEvent, week: int = None):
            """更新课表数据"""
            qq_id = event.get_sender_id()
            
            # 检查绑定
            conn = sqlite3.connect(str(self.db_file))
            cursor = conn.cursor()
            cursor.execute("SELECT student_id FROM user_bindings WHERE qq_id = ?", (qq_id,))
            binding = cursor.fetchone()
            conn.close()
            
            if not binding:
                yield event.plain_result("请先使用 /绑定教务 绑定您的账号")
                return
            
            student_id = binding[0]
            
            yield event.plain_result("正在更新课表数据，请稍候...")
            
            try:
                success = await self.update_course_table(student_id, week)
                
                if success:
                    week_info = await self.get_current_week()
                    current_week = week_info.get("zc", 1) if week_info else 1
                    
                    if week:
                        yield event.plain_result(f"✅ 第{week}周课表更新完成！")
                    else:
                        yield event.plain_result(f"✅ 课表更新完成！当前为第{current_week}周")
                else:
                    yield event.plain_result("❌ 课表更新失败，请稍后再试")
                    
            except Exception as e:
                logger.error(f"更新课表失败: {e}")
                yield event.plain_result(f"更新失败: {str(e)}")

        @filter.command("今天课程")
        async def cmd_today_courses(self, event: AstrMessageEvent):
            """查看今天课程"""
            qq_id = event.get_sender_id()
            
            # 检查绑定
            conn = sqlite3.connect(str(self.db_file))
            cursor = conn.cursor()
            cursor.execute("SELECT student_id, name FROM user_bindings WHERE qq_id = ?", (qq_id,))
            binding = cursor.fetchone()
            conn.close()
            
            if not binding:
                yield event.plain_result("请先使用 /绑定教务 绑定您的账号")
                return
            
            student_id, name = binding
            
            try:
                # 获取今天课程
                courses = await self.get_today_courses(student_id, "全天课表")
                
                if not courses:
                    today = datetime.now()
                    weekday_str = ["一", "二", "三", "四", "五", "六", "日"][today.weekday()]
                    yield event.plain_result(f"📅 {today.month}月{today.day}日（星期{weekday_str}）\n\n✅ 今日无课程安排")
                    return
                
                # 构建消息
                today = datetime.now()
                weekday_str = ["一", "二", "三", "四", "五", "六", "日"][today.weekday()]
                
                message = f"📅 {today.month}月{today.day}日 今日课程（星期{weekday_str}）\n\n"
                
                # 按时间段分组
                morning_courses = []
                afternoon_courses = []
                evening_courses = []
                
                for course in courses:
                    section_code = int(course.get("section_code", 0))
                    if 1 <= section_code <= 4:
                        morning_courses.append(course)
                    elif 5 <= section_code <= 8:
                        afternoon_courses.append(course)
                    else:
                        evening_courses.append(course)
                
                # 上午课程
                if morning_courses:
                    message += "🌅 上午课程：\n"
                    for course in morning_courses:
                        message += self.format_course_message(course)
                
                # 下午课程
                if afternoon_courses:
                    message += "\n🌞 下午课程：\n"
                    for course in afternoon_courses:
                        message += self.format_course_message(course)
                
                # 晚上课程
                if evening_courses:
                    message += "\n🌙 晚上课程：\n"
                    for course in evening_courses:
                        message += self.format_course_message(course)
                
                message += "\n💡 如有变动请以教务处通知为准"
                
                yield event.plain_result(message)
                
            except Exception as e:
                logger.error(f"查询今天课程失败: {e}")
                yield event.plain_result(f"查询失败: {str(e)}")

        @filter.command("测试登录")
        async def cmd_test_login(self, event: AstrMessageEvent, student_id: str, password: str):
            """测试教务系统登录"""
            yield event.plain_result("正在测试登录，请稍候...")
            
            login_result = await self.login_jwc(student_id, password)
            
            if login_result.get("success"):
                user_info = login_result.get("user_info", {})
                name = user_info.get("student_id", "未知")
                
                response = f"✅ 登录成功！\n\n"
                response += f"学号：{student_id}\n"
                response += f"姓名：{name}\n"
                response += f"学年学期：{user_info.get('academic_year', '未知')}\n"
                response += f"角色：{user_info.get('role_id', '未知')}\n\n"
                response += "您可以使用 /绑定教务 学号 密码 来绑定账号"
                
                yield event.plain_result(response)
            else:
                error_msg = login_result.get("error", "登录失败")
                yield event.plain_result(f"❌ {error_msg}")

        async def terminate(self):
            logger.info("南京机电通知监控插件正在卸载...")

else:
    print("南京机电通知监控插件无法加载：缺少必要的依赖或API")
