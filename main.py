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
import urllib.parse
from datetime import datetime, timedelta
from typing import List, Dict, Any, Optional
from pathlib import Path

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
        "2.2.0"
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
            
            # RSA公钥用于密码加密
            self.rsa_public_key = """-----BEGIN PUBLIC KEY-----
MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQC9zpr1gSa3gBnHLeDxw6DuPtnLC9HI8JOQrBbFV3ZkX0V92klvJDwS5YuZ810ZJL8MWED0gRSigS5YvXcQMyxizpN3IV9qhrlb48nI6mua1Xv75J9FxejEWA/kYlkkElwmXbyEMr1eGbYFTko40k82diw7k/xU4PaLnjFgQveSiQIDAQAB
-----END PUBLIC KEY-----"
            
            # 新增教务系统配置
            self.jwc_config = self.config.get("jwc_config", {
                "base_url": "https://nimt.jw.chaoxing.com",
                "login_url": "/admin/login",
                "course_push_times": [
                    {"hour": 7, "minute": 0, "type": "全天课表"},
                    {"hour": 12, "minute": 0, "type": "下午课表"}
                ],
                "enable_course_push": True,
                "course_check_interval": 1440,
                "enable_change_detection": True,
                "change_check_day": 0,
                "change_check_time": "21:00",
                "timeout": 30,
                "max_retries": 3,
                "use_rsa_encryption": True  # 新增：是否使用RSA加密
            })

            self.init_database()
            self.start_scheduler()

            logger.info("南京机电通知监控插件初始化完成")

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
                    "login_url": "/admin/login",
                    "course_push_times": [
                        {"hour": 7, "minute": 0, "type": "全天课表"},
                        {"hour": 12, "minute": 0, "type": "下午课表"}
                    ],
                    "enable_course_push": True,
                    "course_check_interval": 1440,
                    "enable_change_detection": True,
                    "change_check_day": 0,
                    "change_check_time": "21:00",
                    "timeout": 30,
                    "max_retries": 3,
                    "use_rsa_encryption": True
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
                            elif isinstance(value, dict):
                                # 合并嵌套配置
                                for sub_key, sub_value in value.items():
                                    if sub_key not in config[key]:
                                        config[key][sub_key] = sub_value
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
                    check_day = self.jwc_config.get("change_check_day", 0)
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

        # ==================== RSA加密相关方法 ====================
        
        def rsa_encrypt_password(self, password: str) -> str:
            """使用RSA加密密码"""
            try:
                import rsa
                pub_key = rsa.PublicKey.load_pkcs1_openssl_pem(self.rsa_public_key.encode())
                encrypted = rsa.encrypt(password.encode(), pub_key)
                return base64.b64encode(encrypted).decode()
            except ImportError:
                logger.error("需要rsa库，请安装: pip install rsa")
                # 降级方案：返回base64编码的密码
                return base64.b64encode(password.encode()).decode()
            except Exception as e:
                logger.error(f"RSA加密失败: {e}")
                # 降级方案：返回base64编码的密码
                return base64.b64encode(password.encode()).decode()

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

        async def fetch_page(self, url: str, method: str = "GET", data: Dict = None, 
                           cookies: Dict = None, headers: Dict = None) -> str:
            """通用请求函数"""
            default_headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                "Accept-Encoding": "gzip, deflate, br",
                "Connection": "keep-alive",
                "Upgrade-Insecure-Requests": "1"
            }
            
            if headers:
                default_headers.update(headers)

            timeout = aiohttp.ClientTimeout(total=self.jwc_config.get("timeout", 30))
            
            try:
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    if method.upper() == "GET":
                        async with session.get(url, headers=default_headers, cookies=cookies) as response:
                            response.raise_for_status()
                            return await response.text(encoding='utf-8')
                    else:
                        async with session.post(url, headers=default_headers, data=data, cookies=cookies) as response:
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

        async def fetch_jwc_with_cookies(self, url: str, method: str = "GET", data: Dict = None, 
                                        cookies: Dict = None, headers: Dict = None, 
                                        allow_redirects: bool = True) -> Dict:
            """请求教务系统API，支持cookie管理和重定向"""
            base_url = self.jwc_config.get("base_url", "https://nimt.jw.chaoxing.com")
            full_url = base_url + url if url.startswith("/") else url
            
            default_headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                "Content-Type": "application/x-www-form-urlencoded",
                "Origin": base_url,
                "Referer": f"{base_url}/",
                "X-Requested-With": "XMLHttpRequest"
            }
            
            if headers:
                default_headers.update(headers)

            timeout = aiohttp.ClientTimeout(total=self.jwc_config.get("timeout", 30))
            max_retries = self.jwc_config.get("max_retries", 3)

            try:
                # 创建支持cookie的session
                jar = aiohttp.CookieJar(unsafe=True)
                async with aiohttp.ClientSession(timeout=timeout, cookie_jar=jar) as session:
                    
                    # 如果有传入的cookies，手动设置到session中
                    if cookies:
                        for name, value in cookies.items():
                            session.cookie_jar.update_cookies({name: value})
                    
                    if method.upper() == "GET":
                        async with session.get(full_url, headers=default_headers, allow_redirects=allow_redirects) as response:
                            response_text = await response.text()
                            status = response.status
                            response_cookies = session.cookie_jar.filter_cookies(full_url)
                    else:
                        async with session.post(full_url, headers=default_headers, data=data, allow_redirects=allow_redirects) as response:
                            response_text = await response.text()
                            status = response.status
                            response_cookies = session.cookie_jar.filter_cookies(full_url)

                    # 记录请求日志
                    self.log_request(None, full_url, data, response_text, status)
                    
                    # 处理cookie，转换为字典
                    cookie_dict = {}
                    for cookie in response_cookies.values():
                        cookie_dict[cookie.key] = cookie.value
                    
                    if status == 200 or status == 302:
                        # 302是重定向，对登录来说是成功的
                        try:
                            # 尝试解析JSON
                            json_data = json.loads(response_text)
                            return {
                                "ret": 0,
                                "msg": "success",
                                "data": json_data,
                                "cookies": cookie_dict,
                                "status": status
                            }
                        except:
                            # 如果不是JSON，返回原始文本
                            return {
                                "ret": 0,
                                "msg": "success",
                                "data": response_text,
                                "cookies": cookie_dict,
                                "status": status
                            }
                    else:
                        return {
                            "ret": -1,
                            "msg": f"请求失败: {status}",
                            "data": None,
                            "cookies": None,
                            "status": status
                        }
                        
            except Exception as e:
                logger.error(f"请求教务系统失败 {full_url}: {e}")
                return {
                    "ret": -1,
                    "msg": f"网络错误: {str(e)}",
                    "data": None,
                    "cookies": None,
                    "status": 0
                }

        def log_request(self, student_id: str, url: str, request_data: Dict, response_data: str, status_code: int):
            """记录请求日志"""
            try:
                conn = sqlite3.connect(str(self.db_file))
                cursor = conn.cursor()
                
                # 敏感信息脱敏
                safe_request_data = None
                if request_data:
                    safe_data = request_data.copy()
                    if 'password' in safe_data:
                        safe_data['password'] = '***HIDDEN***'
                    safe_request_data = json.dumps(safe_data)
                
                # 限制响应数据长度
                safe_response_data = None
                if response_data:
                    if len(response_data) > 1000:
                        safe_response_data = response_data[:1000] + "...[truncated]"
                    else:
                        safe_response_data = response_data
                
                cursor.execute(
                    """
                    INSERT INTO request_logs (student_id, api_url, request_data, response_data, status_code)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (student_id, url, safe_request_data, safe_response_data, status_code)
                )
                
                conn.commit()
                conn.close()
            except Exception as e:
                logger.error(f"记录请求日志失败: {e}")

        async def login_jwc_rsa(self, student_id: str, password: str) -> Dict[str, Any]:
            """使用RSA加密的登录方法"""
            try:
                # 加密密码
                encrypted_password = self.rsa_encrypt_password(password)
                
                login_url = f"{self.jwc_config['base_url']}{self.jwc_config['login_url']}"
                
                # 构建登录数据
                login_data = {
                    'username': student_id,
                    'password': encrypted_password,
                    'vcode': '',
                    'jcaptchaCode': '',
                    'rememberMe': ''
                }
                
                logger.info(f"使用RSA加密登录，学号: {student_id}")
                
                # 发送登录请求
                result = await self.fetch_jwc_with_cookies(
                    self.jwc_config['login_url'],
                    method="POST",
                    data=login_data,
                    allow_redirects=False
                )
                
                status = result.get('status')
                cookies = result.get('cookies', {})
                
                logger.info(f"RSA登录响应状态: {status}")
                logger.info(f"RSA登录cookies: {list(cookies.keys()) if cookies else '空'}")
                
                # 检查是否登录成功
                if status == 302:
                    # 302重定向表示登录成功
                    if cookies and any(key in cookies for key in ['username', 'puid', 'jw_uf']):
                        logger.info("RSA登录成功！获得有效cookies")
                        return {
                            "success": True,
                            "student_id": student_id,
                            "cookies": cookies,
                            "message": "登录成功"
                        }
                    else:
                        # 即使没有关键cookie，也可能是登录成功了
                        logger.warning("RSA登录返回302但未获得关键cookies，尝试访问主页验证")
                        
                        # 尝试访问主页验证
                        test_result = await self.fetch_jwc_with_cookies(
                            "/admin/main",
                            method="GET",
                            cookies=cookies
                        )
                        
                        if test_result.get('status') == 200:
                            logger.info("验证成功，可以访问主页")
                            return {
                                "success": True,
                                "student_id": student_id,
                                "cookies": cookies,
                                "message": "登录成功"
                            }
                        else:
                            return {"success": False, "error": "登录失败：未获得有效会话"}
                elif status == 200:
                    data = result.get("data", "")
                    if isinstance(data, str):
                        if "账号或密码错误" in data:
                            return {"success": False, "error": "账号或密码错误"}
                        elif "验证码" in data:
                            return {"success": False, "error": "需要验证码，请稍后再试"}
                        else:
                            # 返回响应文本供调试
                            return {"success": False, "error": f"登录失败，服务器返回: {data[:200]}"}
                    else:
                        return {"success": False, "error": "登录失败：未知错误"}
                else:
                    return {"success": False, "error": f"登录失败: 状态码 {status}"}
                    
            except Exception as e:
                logger.error(f"RSA登录失败: {e}")
                return {"success": False, "error": f"登录失败: {str(e)}"}

        async def login_jwc_direct(self, student_id: str, password: str) -> Dict[str, Any]:
            """备用登录方法：直接POST请求"""
            try:
                login_url = f"{self.jwc_config['base_url']}{self.jwc_config['login_url']}"
                
                # 构建完整的表单数据
                form_data = {
                    "username": student_id,
                    "password": password,
                    "vcode": "",
                    "jcaptchaCode": "",
                    "rememberMe": ""
                }
                
                headers = {
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Origin": self.jwc_config['base_url'],
                    "Referer": login_url,
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
                    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                    "Cache-Control": "no-cache",
                    "Connection": "keep-alive",
                    "Upgrade-Insecure-Requests": "1"
                }
                
                logger.info(f"尝试直接登录，学号: {student_id}")
                
                # 创建session
                jar = aiohttp.CookieJar(unsafe=True)
                timeout = aiohttp.ClientTimeout(total=self.jwc_config.get("timeout", 30))
                
                async with aiohttp.ClientSession(timeout=timeout, cookie_jar=jar) as session:
                    # 先GET一次登录页面，获取初始cookie
                    async with session.get(login_url, headers=headers) as response:
                        await response.text()
                    
                    # 发送POST请求，不自动跟随重定向
                    async with session.post(
                        login_url, 
                        data=form_data, 
                        headers=headers,
                        allow_redirects=False
                    ) as response:
                        status = response.status
                        response_cookies = session.cookie_jar.filter_cookies(login_url)
                        
                        # 处理cookie，转换为字典
                        cookie_dict = {}
                        for cookie in response_cookies.values():
                            cookie_dict[cookie.key] = cookie.value
                        
                        logger.info(f"直接登录响应状态: {status}")
                        logger.info(f"直接登录cookies: {cookie_dict}")
                        
                        if status == 302:
                            # 检查是否有重定向到成功页面
                            location = response.headers.get("location", "")
                            if "main.html" in location or "admin" in location:
                                # 登录成功
                                return {
                                    "success": True,
                                    "student_id": student_id,
                                    "cookies": cookie_dict,
                                    "message": "登录成功"
                                }
                            else:
                                return {"success": False, "error": f"登录失败，重定向到: {location}"}
                        elif status == 200:
                            # 读取响应内容检查错误
                            response_text = await response.text()
                            if "账号或密码错误" in response_text:
                                return {"success": False, "error": "账号或密码错误"}
                            else:
                                return {"success": False, "error": "登录失败，未知原因"}
                        else:
                            return {"success": False, "error": f"登录失败，状态码: {status}"}
                            
            except Exception as e:
                logger.error(f"直接登录失败: {e}")
                return {"success": False, "error": f"登录失败: {str(e)}"}

        async def login_jwc(self, student_id: str, password: str) -> Dict[str, Any]:
            """主登录函数 - 优先使用RSA加密登录"""
            logger.info(f"开始登录流程，学号: {student_id}")
            
            use_rsa = self.jwc_config.get("use_rsa_encryption", True)
            
            if use_rsa:
                # 首先尝试RSA加密登录
                result = await self.login_jwc_rsa(student_id, password)
                
                if result.get("success"):
                    return result
                else:
                    logger.info("RSA登录失败，尝试直接登录...")
            
            # 如果RSA登录失败或禁用RSA，尝试直接登录
            direct_result = await self.login_jwc_direct(student_id, password)
            
            if direct_result.get("success"):
                return direct_result
            else:
                # 返回错误信息
                error_msg = direct_result.get("error", "登录失败")
                return {"success": False, "error": error_msg}

        async def get_user_info_with_cookies(self, cookies: Dict) -> Dict[str, Any]:
            """使用cookies获取用户信息"""
            try:
                # 使用cookies请求当前周次信息
                today = datetime.now().strftime("%Y-%m-%d")
                result = await self.fetch_jwc_with_cookies(
                    f"/admin/getDayBz?rq={today}",
                    method="GET",
                    cookies=cookies
                )
                
                if result.get("ret") == 0 and result.get("data"):
                    data = result.get("data")
                    if isinstance(data, dict) and "xlrq" in data:
                        xlrq = data["xlrq"]
                        return {
                            "student_id": xlrq.get("currentUserName"),
                            "user_id": xlrq.get("currentUserId"),
                            "role_id": xlrq.get("currentRoleId"),
                            "academic_year": xlrq.get("xnxqh"),
                            "department_id": xlrq.get("currentDepartmentId")
                        }
                    else:
                        # 尝试直接解析
                        try:
                            if isinstance(data, str):
                                data_json = json.loads(data)
                                if "xlrq" in data_json:
                                    xlrq = data_json["xlrq"]
                                    return {
                                        "student_id": xlrq.get("currentUserName"),
                                        "user_id": xlrq.get("currentUserId"),
                                        "role_id": xlrq.get("currentRoleId"),
                                        "academic_year": xlrq.get("xnxqh"),
                                        "department_id": xlrq.get("currentDepartmentId")
                                    }
                        except:
                            pass
                return None
            except Exception as e:
                logger.error(f"获取用户信息失败: {e}")
                return None

        async def get_current_week_with_cookies(self, cookies: Dict, date_str: str = None) -> Dict[str, Any]:
            """使用cookies获取当前周次信息"""
            try:
                if not date_str:
                    date_str = datetime.now().strftime("%Y-%m-%d")
                
                result = await self.fetch_jwc_with_cookies(
                    f"/admin/getDayBz?rq={date_str}",
                    method="GET",
                    cookies=cookies
                )
                
                if result.get("ret") == 0 and result.get("data"):
                    data = result.get("data")
                    if isinstance(data, dict):
                        return data.get("xlrq", {})
                    elif isinstance(data, str):
                        try:
                            data_json = json.loads(data)
                            return data_json.get("xlrq", {})
                        except:
                            return {}
                return None
            except Exception as e:
                logger.error(f"获取周次信息失败: {e}")
                return None

        async def get_week_days_with_cookies(self, cookies: Dict, week: int) -> List[Dict[str, Any]]:
            """使用cookies获取周次对应的星期"""
            try:
                result = await self.fetch_jwc_with_cookies(
                    "/admin/getXqByZc",
                    method="POST",
                    data={"zc": week},
                    cookies=cookies
                )
                
                if result.get("ret") == 0 and result.get("data"):
                    return result.get("data", [])
                return []
            except Exception as e:
                logger.error(f"获取星期信息失败: {e}")
                return []

        async def get_course_table_with_cookies(self, cookies: Dict, week: int, student_id: str = None) -> Dict[str, Any]:
            """使用cookies获取课表"""
            try:
                result = await self.fetch_jwc_with_cookies(
                    "/admin/getXsdSykb",
                    method="POST",
                    data={"type": 1, "zc": week},
                    cookies=cookies
                )
                
                if result.get("ret") == 0 and result.get("data"):
                    # 处理课表数据
                    course_data = result.get("data", {})
                    
                    # 提取学术周信息
                    academic_year = None
                    week_info = await self.get_current_week_with_cookies(cookies)
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
                                "course_type": "理论",
                                "hours": hours,
                                "is_practice": False,
                                "week_range": "",
                                "course_hash": course_hash
                            }
                            
                            courses.append(course)
                
                return courses
                
            except Exception as e:
                logger.error(f"解析课表失败: {e}")
                return []

        def get_section_time(self, section_code: str) -> Dict[str, str]:
            """根据节次编码获取时间"""
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

        async def save_user_cookies(self, qq_id: str, student_id: str, cookies: Dict):
            """保存用户cookies到数据库"""
            try:
                conn = sqlite3.connect(str(self.db_file))
                cursor = conn.cursor()
                
                # 更新用户绑定表中的cookie字段
                cursor.execute(
                    """
                    UPDATE user_bindings 
                    SET cookie = ?, last_login = ?
                    WHERE qq_id = ? AND student_id = ?
                    """,
                    (json.dumps(cookies), datetime.now().strftime("%Y-%m-%d %H:%M:%S"), qq_id, student_id)
                )
                
                # 保存到登录会话表
                cursor.execute(
                    """
                    INSERT OR REPLACE INTO login_sessions 
                    (student_id, cookies, last_login, expires_at, status)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        student_id,
                        json.dumps(cookies),
                        datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        (datetime.now() + timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S"),
                        "active"
                    )
                )
                
                conn.commit()
                conn.close()
                logger.info(f"成功保存用户{student_id}的cookies")
                
            except Exception as e:
                logger.error(f"保存用户cookies失败: {e}")

        async def get_user_cookies(self, student_id: str) -> Dict:
            """从数据库获取用户cookies"""
            try:
                conn = sqlite3.connect(str(self.db_file))
                cursor = conn.cursor()
                
                cursor.execute(
                    "SELECT cookies FROM login_sessions WHERE student_id = ? AND status = 'active'",
                    (student_id,)
                )
                
                result = cursor.fetchone()
                conn.close()
                
                if result and result[0]:
                    return json.loads(result[0])
                return {}
                
            except Exception as e:
                logger.error(f"获取用户cookies失败: {e}")
                return {}

        async def update_course_table_with_cookies(self, student_id: str, cookies: Dict, week: int = None):
            """使用cookies更新课表数据"""
            try:
                # 如果没有指定周次，获取当前周次
                if not week:
                    week_info = await self.get_current_week_with_cookies(cookies)
                    if week_info:
                        week = week_info.get("zc", 1)
                    else:
                        week = 1
                
                # 获取课表数据
                course_result = await self.get_course_table_with_cookies(cookies, week, student_id)
                
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
                        # 获取用户cookies
                        cookies = await self.get_user_cookies(student_id)
                        
                        if not cookies:
                            logger.warning(f"用户{student_id}没有有效的cookies，跳过推送")
                            continue
                        
                        # 获取当前周次信息
                        week_info = await self.get_current_week_with_cookies(cookies)
                        if not week_info:
                            logger.warning(f"无法获取用户{student_id}的周次信息")
                            continue
                        
                        week = week_info.get("zc", 1)
                        day_of_week = week_info.get("xqbh", 1)
                        
                        # 从数据库获取今天课程
                        conn = sqlite3.connect(str(self.db_file))
                        cursor = conn.cursor()
                        
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
                        
                        if not rows:
                            # 如果没有课程数据，尝试更新
                            await self.update_course_table_with_cookies(student_id, cookies, week)
                            continue
                        
                        # 转换为字典列表
                        columns = [description[0] for description in cursor.description]
                        courses = [dict(zip(columns, row)) for row in rows]
                        
                        # 根据推送类型过滤课程
                        if push_type == "下午课表":
                            # 只返回下午及晚上的课程（节次5-11）
                            courses = [c for c in courses if c.get("section_code") and int(c["section_code"]) >= 5]
                        
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
                        # 获取用户cookies
                        cookies = await self.get_user_cookies(student_id)
                        
                        if cookies:
                            # 更新最新课表
                            await self.update_course_table_with_cookies(student_id, cookies)
                        
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
            
            # 尝试登录
            login_result = await self.login_jwc(student_id, password)
            
            if login_result.get("success"):
                # 绑定成功，保存信息
                try:
                    # 获取cookies
                    cookies = login_result.get("cookies", {})
                    
                    # 获取用户信息
                    user_info = await self.get_user_info_with_cookies(cookies)
                    name = user_info.get("student_id", "") if user_info else ""
                    
                    # 保存加密密码
                    encoded_password = base64.b64encode(password.encode()).decode()
                    
                    conn = sqlite3.connect(str(self.db_file))
                    cursor = conn.cursor()
                    
                    # 保存用户绑定信息
                    cursor.execute(
                        """
                        INSERT INTO user_bindings (qq_id, student_id, password, name, bind_time, cookie)
                        VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (
                            qq_id, 
                            student_id, 
                            encoded_password, 
                            name, 
                            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                            json.dumps(cookies)
                        )
                    )
                    
                    conn.commit()
                    conn.close()
                    
                    # 保存cookies到会话表
                    await self.save_user_cookies(qq_id, student_id, cookies)
                    
                    # 更新课表数据
                    yield event.plain_result("验证成功，正在更新课表数据...")
                    success = await self.update_course_table_with_cookies(student_id, cookies)
                    
                    if success:
                        yield event.plain_result(f"✅ 绑定成功！\n学号：{student_id}\n姓名：{name}\n\n课表数据已更新，明天开始将为您推送课程提醒。")
                    else:
                        yield event.plain_result(f"✅ 绑定成功！\n学号：{student_id}\n姓名：{name}\n\n注意：课表数据更新失败，请稍后使用 /更新课表 手动更新。")
                    
                except Exception as e:
                    logger.error(f"保存绑定信息失败: {e}")
                    yield event.plain_result(f"绑定成功但保存信息失败: {str(e)}\n请稍后使用 /更新课表 刷新数据。")
            else:
                error_msg = login_result.get("error", "绑定失败")
                yield event.plain_result(f"❌ {error_msg}\n\n可能的原因：\n1. 学号或密码错误\n2. 需要验证码（请稍后再试）\n3. 网络问题\n\n请检查后重试。")

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
                
                student_id = existing[0]
                
                cursor.execute("DELETE FROM user_bindings WHERE qq_id = ?", (qq_id,))
                cursor.execute("DELETE FROM course_schedules WHERE student_id = ?", (student_id,))
                cursor.execute("DELETE FROM login_sessions WHERE student_id = ?", (student_id,))
                
                conn.commit()
                conn.close()
                
                yield event.plain_result("✅ 解绑成功！已清除您的绑定信息、课表数据和登录会话。")
                
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
                    "SELECT student_id, name, class_name, bind_time, cookie FROM user_bindings WHERE qq_id = ?",
                    (qq_id,)
                )
                
                binding = cursor.fetchone()
                conn.close()
                
                if not binding:
                    yield event.plain_result("您尚未绑定教务系统")
                    return
                
                student_id, name, class_name, bind_time, cookie_json = binding
                
                response = f"📋 绑定信息\n\n"
                response += f"QQ号：{qq_id}\n"
                response += f"学号：{student_id}\n"
                if name:
                    response += f"姓名：{name}\n"
                if class_name:
                    response += f"班级：{class_name}\n"
                response += f"绑定时间：{bind_time}\n"
                
                # 检查是否有有效的cookies
                has_cookies = False
                if cookie_json:
                    try:
                        cookies = json.loads(cookie_json)
                        if cookies:
                            has_cookies = True
                            cookie_count = len(cookies)
                            response += f"会话状态：✅ 有效（{cookie_count}个cookies）\n"
                    except:
                        pass
                
                if not has_cookies:
                    response += "会话状态：❌ 无效或过期\n"
                
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
                    response += f"\n📅 已存储 {count} 条课程记录\n"
                else:
                    response += f"\n📅 暂无课表数据，请使用 /更新课表 获取\n"
                
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
                # 获取用户cookies
                cookies = await self.get_user_cookies(student_id)
                
                if not cookies:
                    yield event.plain_result("您的登录会话已过期，请重新绑定或使用 /更新课表 刷新")
                    return
                
                # 获取当前周次
                week_info = await self.get_current_week_with_cookies(cookies)
                
                if not week_info:
                    yield event.plain_result("无法获取周次信息，请稍后再试")
                    return
                
                current_week = week_info.get("zc", 1)
                academic_year = week_info.get("xnxqh", "")
                
                # 确定要查询的周次
                if week is None:
                    # 如果是周六或周日，查看下周课表
                    today = datetime.now()
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
                    success = await self.update_course_table_with_cookies(student_id, cookies, query_week)
                    
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
                week_days = await self.get_week_days_with_cookies(cookies, query_week)
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
            
            # 获取用户cookies
            cookies = await self.get_user_cookies(student_id)
            
            if not cookies:
                # 尝试重新登录
                conn = sqlite3.connect(str(self.db_file))
                cursor = conn.cursor()
                cursor.execute("SELECT password FROM user_bindings WHERE qq_id = ? AND student_id = ?", (qq_id, student_id))
                result = cursor.fetchone()
                conn.close()
                
                if not result:
                    yield event.plain_result("找不到您的登录信息，请重新绑定")
                    return
                
                # 解码密码
                try:
                    encoded_password = result[0]
                    password = base64.b64decode(encoded_password).decode()
                    
                    yield event.plain_result("会话过期，正在重新登录...")
                    login_result = await self.login_jwc(student_id, password)
                    
                    if not login_result.get("success"):
                        yield event.plain_result("重新登录失败，请重新绑定")
                        return
                    
                    cookies = login_result.get("cookies", {})
                    await self.save_user_cookies(qq_id, student_id, cookies)
                    
                except Exception as e:
                    logger.error(f"重新登录失败: {e}")
                    yield event.plain_result("重新登录失败，请重新绑定")
                    return
            
            yield event.plain_result("正在更新课表数据，请稍候...")
            
            try:
                success = await self.update_course_table_with_cookies(student_id, cookies, week)
                
                if success:
                    week_info = await self.get_current_week_with_cookies(cookies)
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
                # 获取用户cookies
                cookies = await self.get_user_cookies(student_id)
                
                if not cookies:
                    yield event.plain_result("您的登录会话已过期，请使用 /更新课表 刷新")
                    return
                
                # 获取当前周次
                week_info = await self.get_current_week_with_cookies(cookies)
                if not week_info:
                    yield event.plain_result("无法获取周次信息，请稍后再试")
                    return
                
                week = week_info.get("zc", 1)
                day_of_week = week_info.get("xqbh", 1)
                
                # 从数据库获取课程
                conn = sqlite3.connect(str(self.db_file))
                cursor = conn.cursor()
                
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
                
                if not rows:
                    yield event.plain_result("今天没有课程安排")
                    return
                
                # 转换为字典列表
                columns = [description[0] for description in cursor.description]
                courses = [dict(zip(columns, row)) for row in rows]
                
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
            
            # 尝试登录
            login_result = await self.login_jwc(student_id, password)
            
            if login_result.get("success"):
                cookies = login_result.get("cookies", {})
                cookie_count = len(cookies)
                
                response = f"✅ 登录成功！\n\n"
                response += f"学号：{student_id}\n"
                response += f"获得cookies：{cookie_count}个\n"
                response += f"关键cookies："
                
                # 显示关键cookies
                important_keys = ['username', 'puid', 'jw_uf', 'initPass', 'defaultPass']
                for key in important_keys:
                    if key in cookies:
                        value = cookies[key]
                        if len(value) > 20:
                            value = value[:20] + "..."
                        response += f"\n  {key}: {value}"
                
                response += f"\n\n提示信息：{login_result.get('message', '登录成功')}\n\n"
                response += "您可以使用 /绑定教务 学号 密码 来绑定账号"
                
                yield event.plain_result(response)
            else:
                error_msg = login_result.get("error", "登录失败")
                yield event.plain_result(f"❌ {error_msg}\n\n请检查：\n1. 学号和密码是否正确\n2. 密码是否包含特殊字符\n3. 是否开启了验证码")

        async def terminate(self):
            logger.info("南京机电通知监控插件正在卸载...")

else:
    print("南京机电通知监控插件无法加载：缺少必要的依赖或API")
