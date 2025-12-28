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
import rsa
from datetime import datetime, timedelta
from typing import List, Dict, Any, Optional
from pathlib import Path
from urllib.parse import urljoin

try:
    import aiohttp
    from bs4 import BeautifulSoup
    import astrbot.api.message_components as Comp

    HAS_DEPENDENCIES = True
except ImportError as e:
    print(f"缺少依赖: {e}")
    HAS_DEPENDENCIES = False

try:
    from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult
    from astrbot.api.star import Context, Star, register
    from astrbot.api import logger
    from astrbot.api.provider import ProviderRequest, LLMResponse

    HAS_ASTRBOT_API = True
except ImportError as e:
    print(f"AstrBot API导入失败: {e}")
    HAS_ASTRBOT_API = False

if HAS_DEPENDENCIES and HAS_ASTRBOT_API:
    @register(
        "nimt_notice_monitor",
        "南京机电职业技术学院通知监控插件",
        "2.1.0",
        "https://github.com/AstrBotDevs/astrbot_plugin_nimt_notice_monitor"
    )
    class NJIMTNoticeMonitor(Star):
        def __init__(self, context: Context):
            super().__init__(context)

            # 使用AstrBot的路径API获取数据目录
            from astrbot.core.utils.astrbot_path import get_astrbot_data_path
            astrbot_data_path = get_astrbot_data_path()

            self.data_dir = astrbot_data_path / "plugin_data" / "nimt_notice_monitor"
            self.data_dir.mkdir(parents=True, exist_ok=True)

            self.db_file = self.data_dir / "notices.db"
            self.config_file = self.data_dir / "config.json"

            self.config = self.load_config()
            self.sites_config = self.config.get("sites_config", [])
            self.push_targets = self.config.get("push_targets", {"users": [], "groups": []})
            self.check_interval = self.config.get("check_interval", 300)  # 默认5分钟

            # 教务系统配置
            self.jwc_config = self.config.get("jwc_config", {
                "base_url": "https://nimt.jw.chaoxing.com",
                "login_url": "/admin/login",
                "timeout": 30,
                "public_key": """-----BEGIN PUBLIC KEY-----
MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQC9zpr1gSa3gBnHLeDxw6DuPtnLC9HI8JOQrBbFV3ZkX0V92klvJDwS5YuZ810ZJL8MWED0gRSigS5YvXcQMyxizpN3IV9qhrlb48nI6mua1Xv75J9FxejEWA/kYlkkElwmXbyEMr1eGbYFTko40k82diw7k/xU4PaLnjFgQveSiQIDAQAB
-----END PUBLIC KEY-----"""
            })

            self.init_database()
            self.start_scheduler()

            logger.info("南京机电通知监控插件初始化完成")

        def load_config(self) -> Dict[str, Any]:
            """加载配置文件"""
            default_config = {
                "sites_config": [
                    {
                        "id": "nimt_main",
                        "name": "南京机电职业技术学院",
                        "url": "https://www.nimt.edu.cn/739/list.htm",
                        "base_url": "https://www.nimt.edu.cn",
                        "remark": "学校主站通知公告",
                        "enabled": True,
                        "selector": "ul.news_list"
                    },
                    {
                        "id": "jiaowu",
                        "name": "教务处",
                        "url": "https://www.nimt.edu.cn/jiaowu/396/list.htm",
                        "base_url": "https://www.nimt.edu.cn",
                        "remark": "教务处通知公告",
                        "enabled": True,
                        "selector": "ul.news_list"
                    },
                    {
                        "id": "xinxi",
                        "name": "信息工程系",
                        "url": "https://www.nimt.edu.cn/xinxi/tzgg/list.htm",
                        "base_url": "https://www.nimt.edu.cn",
                        "remark": "信息工程系通知公告",
                        "enabled": True,
                        "selector": "ul.wp_list"
                    },
                    {
                        "id": "landao",
                        "name": "蓝岛创客空间",
                        "url": "https://www.nimt.edu.cn/landao/19517/list.htm",
                        "base_url": "https://www.nimt.edu.cn",
                        "remark": "蓝岛创客空间通知公告",
                        "enabled": True,
                        "selector": "ul.list-paddingleft-2"
                    }
                ],
                "push_targets": {
                    "users": [],
                    "groups": []
                },
                "check_interval": 300,
                "jwc_config": {
                    "base_url": "https://nimt.jw.chaoxing.com",
                    "login_url": "/admin/login",
                    "timeout": 30,
                    "public_key": """-----BEGIN PUBLIC KEY-----
MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQC9zpr1gSa3gBnHLeDxw6DuPtnLC9HI8JOQrBbFV3ZkX0V92klvJDwS5YuZ810ZJL8MWED0gRSigS5YvXcQMyxizpN3IV9qhrlb48nI6mua1Xv75J9FxejEWA/kYlkkElwmXbyEMr1eGbYFTko40k82diw7k/xU4PaLnjFgQveSiQIDAQAB
-----END PUBLIC KEY-----"""
                },
                "course_check_interval": 3600,
                "enable_course_monitor": False
            }

            if self.config_file.exists():
                try:
                    with open(self.config_file, 'r', encoding='utf-8') as f:
                        config = json.load(f)
                        # 合并默认配置
                        for key, value in default_config.items():
                            if key not in config:
                                config[key] = value
                        return config
                except Exception as e:
                    logger.error(f"加载配置文件失败: {e}")

            self.save_config(default_config)
            return default_config

        def save_config(self, config: Dict[str, Any] = None):
            """保存配置文件"""
            if config is None:
                config = self.config

            try:
                with open(self.config_file, 'w', encoding='utf-8') as f:
                    json.dump(config, f, ensure_ascii=False, indent=2)
                self.config = config
                self.sites_config = config.get("sites_config", [])
                self.push_targets = config.get("push_targets", {"users": [], "groups": []})
                self.check_interval = config.get("check_interval", 300)
                self.jwc_config = config.get("jwc_config", {})
            except Exception as e:
                logger.error(f"保存配置文件失败: {e}")

        def init_database(self):
            """初始化数据库"""
            try:
                with self.get_db_connection() as conn:
                    cursor = conn.cursor()

                    # 通知表
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

                    # 用户绑定表
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

                    # 课表缓存表
                    cursor.execute("""
                        CREATE TABLE IF NOT EXISTS course_cache (
                            student_id TEXT NOT NULL,
                            week INTEGER NOT NULL,
                            course_data TEXT NOT NULL,
                            update_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                            PRIMARY KEY (student_id, week)
                        )
                    """)

                    # 课程变动记录表
                    cursor.execute("""
                        CREATE TABLE IF NOT EXISTS course_changes (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            student_id TEXT NOT NULL,
                            change_type TEXT NOT NULL,
                            course_name TEXT NOT NULL,
                            old_value TEXT,
                            new_value TEXT,
                            change_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                            notified BOOLEAN DEFAULT 0
                        )
                    """)

                    conn.commit()
                    logger.info("数据库初始化完成")
            except Exception as e:
                logger.error(f"数据库初始化失败: {e}")

        def get_db_connection(self):
            """获取数据库连接（使用上下文管理器）"""
            return sqlite3.connect(str(self.db_file), timeout=30, check_same_thread=False)

        def start_scheduler(self):
            """启动定时任务"""
            try:
                from astrbot.utils.schedule import scheduler

                # 通知检查任务
                @scheduler.scheduled_job('interval', seconds=self.check_interval, id='nimt_check_notices')
                async def scheduled_check():
                    await self.check_all_sites_task()

                # 课程检查任务
                @scheduler.scheduled_job('interval', seconds=self.config.get("course_check_interval", 3600),
                                         id='nimt_check_courses')
                async def scheduled_course_check():
                    if self.config.get("enable_course_monitor", False):
                        await self.check_all_courses_task()

                logger.info("定时任务初始化完成")
            except ImportError:
                logger.warning("未找到调度器,定时任务功能不可用")
            except Exception as e:
                logger.error(f"启动调度器失败: {e}")

        # ==================== 异步HTTP客户端 ====================

        async def fetch_page(self, url: str, method: str = "GET", data: Dict = None,
                             cookies: Dict = None, headers: Dict = None) -> str:
            """通用异步请求函数"""
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

        # ==================== 通知监控功能 ====================

        async def check_all_sites_task(self):
            """定时检查所有网站任务"""
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

        async def check_site_notices(self, site_config: Dict[str, Any]) -> List[Dict[str, Any]]:
            """检查单个网站的通知"""
            new_notices = []

            try:
                html = await self.fetch_page(site_config["url"])
                notices = await self.parse_notices(html, site_config)

                with self.get_db_connection() as conn:
                    cursor = conn.cursor()

                    for notice in notices:
                        cursor.execute("SELECT id FROM notices WHERE id = ?", (notice["id"],))

                        if not cursor.fetchone():
                            cursor.execute(
                                "INSERT INTO notices (id, site_id, title, url, publish_date) VALUES (?, ?, ?, ?, ?)",
                                (notice["id"], notice["site_id"], notice["title"], notice["url"],
                                 notice["publish_date"])
                            )
                            new_notices.append(notice)

                    conn.commit()

            except Exception as e:
                logger.error(f"检查网站 {site_config['name']} 失败: {e}")

            return new_notices

        async def parse_notices(self, html: str, site_config: Dict[str, Any]) -> List[Dict[str, Any]]:
            """解析通知列表页面"""
            if not html:
                return []

            try:
                soup = BeautifulSoup(html, 'html.parser')
                notices = []

                # 优先使用配置的选择器
                selector = site_config.get("selector", "")
                if selector:
                    list_container = soup.select_one(selector)
                else:
                    # 备用选择器
                    selectors = [
                        'ul.news_list',
                        'ul.wp_list',
                        'div.news_list ul',
                        'div.list ul',
                        'div.article-list ul',
                        'ul.list-paddingleft-2'
                    ]

                    list_container = None
                    for sel in selectors:
                        list_container = soup.select_one(sel)
                        if list_container:
                            break

                if not list_container:
                    # 尝试查找新闻列表项
                    news_items = soup.find_all('li', class_=re.compile(r'news|list'))
                    if news_items:
                        items = news_items
                    else:
                        # 查找所有包含链接的列表项
                        items = soup.find_all('li')
                else:
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

                        # 提取发布日期
                        publish_date = datetime.now().strftime("%Y-%m-%d")
                        date_elems = item.find_all(['span', 'div', 'td', 'p'])

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

        async def check_all_sites(self) -> List[Dict[str, Any]]:
            """检查所有网站"""
            all_new_notices = []

            tasks = []
            for site in self.sites_config:
                if site.get("enabled", True):
                    tasks.append(self.check_site_notices(site))

            if tasks:
                results = await asyncio.gather(*tasks, return_exceptions=True)
                for result in results:
                    if isinstance(result, list):
                        all_new_notices.extend(result)

            return all_new_notices

        async def send_notice_push(self, notice: Dict[str, Any]):
            """发送通知推送"""
            try:
                # 构建富媒体消息
                chain = [
                    Comp.Plain("📢 新通知提醒\n\n"),
                    Comp.Plain(f"📝 {notice['remark']}\n") if notice.get("remark") else Comp.Plain(""),
                    Comp.Plain(f"🏫 {notice['site_name']}\n"),
                    Comp.Plain(f"📌 {notice['title']}\n"),
                    Comp.Plain(f"📅 {notice['publish_date']}\n"),
                    Comp.Plain(f"🔗 {notice['url']}\n")
                ]

                # 过滤空消息段
                chain = [msg for msg in chain if not isinstance(msg, Comp.Plain) or msg.text.strip()]

                # 发送给用户
                for user_id in self.push_targets["users"]:
                    try:
                        await self.context.send_message(f"private:{user_id}", chain)
                    except Exception as e:
                        logger.error(f"推送用户 {user_id} 失败: {e}")

                # 发送给群组
                for group_id in self.push_targets["groups"]:
                    try:
                        await self.context.send_message(f"group:{group_id}", chain)
                    except Exception as e:
                        logger.error(f"推送群组 {group_id} 失败: {e}")

                # 标记为已推送
                with self.get_db_connection() as conn:
                    cursor = conn.cursor()
                    cursor.execute(
                        "UPDATE notices SET notified = 1, notified_at = CURRENT_TIMESTAMP WHERE id = ?",
                        (notice["id"],)
                    )
                    conn.commit()

            except Exception as e:
                logger.error(f"发送推送失败: {e}")

        # ==================== 教务系统功能 ====================

        def encrypt_password(self, password: str) -> str:
            """RSA加密密码"""
            try:
                pub_key = rsa.PublicKey.load_pkcs1_openssl_pem(
                    self.jwc_config.get("public_key", "").encode()
                )
                encrypted = rsa.encrypt(password.encode(), pub_key)
                return base64.b64encode(encrypted).decode()
            except Exception as e:
                logger.error(f"密码加密失败: {e}")
                return password

        async def login_jwc(self, student_id: str, password: str) -> Dict[str, Any]:
            """登录教务系统"""
            try:
                base_url = self.jwc_config.get("base_url", "https://nimt.jw.chaoxing.com")
                login_url = f"{base_url}/admin/login"

                async with aiohttp.ClientSession() as session:
                    # 1. 访问登录页面获取初始cookie
                    async with session.get(login_url) as response:
                        if response.status != 200:
                            return {"success": False, "error": "无法访问登录页面"}

                    # 2. 加密密码
                    encrypted_password = self.encrypt_password(password)

                    # 3. 准备登录数据
                    login_data = {
                        'username': student_id,
                        'password': encrypted_password,
                        'vcode': '',
                        'jcaptchaCode': '',
                        'rememberMe': ''
                    }

                    headers = {
                        'Content-Type': 'application/x-www-form-urlencoded',
                        'Origin': base_url,
                        'Referer': login_url,
                        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
                    }

                    # 4. 发送登录请求
                    async with session.post(
                            login_url,
                            data=login_data,
                            headers=headers,
                            allow_redirects=False
                    ) as response:
                        status = response.status

                        # 获取cookies
                        cookies = {}
                        for cookie in session.cookie_jar:
                            cookies[cookie.key] = cookie.value

                        if status == 302:
                            # 登录成功，获取用户信息
                            user_info = await self.get_user_info(session, base_url)
                            return {
                                "success": True,
                                "student_id": student_id,
                                "cookies": cookies,
                                "user_info": user_info,
                                "message": "登录成功"
                            }
                        else:
                            response_text = await response.text()
                            if "账号或密码错误" in response_text:
                                return {"success": False, "error": "账号或密码错误"}
                            elif "验证码" in response_text:
                                return {"success": False, "error": "需要验证码，请稍后再试"}
                            else:
                                return {"success": False, "error": "登录失败，未知原因"}

            except Exception as e:
                logger.error(f"登录失败: {e}")
                return {"success": False, "error": f"登录失败: {str(e)}"}

        async def get_user_info(self, session: aiohttp.ClientSession, base_url: str) -> Dict[str, Any]:
            """获取用户信息"""
            try:
                async with session.get(f"{base_url}/admin/main") as response:
                    if response.status == 200:
                        html = await response.text()

                        # 提取用户信息
                        user_info = {}

                        # 提取姓名
                        name_pattern = r'<span class="admin_name">([^<]+)</span>'
                        name_match = re.search(name_pattern, html)
                        if name_match:
                            user_info['username'] = name_match.group(1).strip()

                        # 提取姓名（从箭头按钮）
                        arrow_pattern = r'<span class="arrowbt">([^<]+)</span>'
                        arrow_match = re.search(arrow_pattern, html)
                        if arrow_match:
                            user_info['name'] = arrow_match.group(1).strip()

                        # 提取院系专业
                        dept_pattern = r'<span class="key add-title">院系</span><span class="value yx">([^<]+)</span>'
                        dept_match = re.search(dept_pattern, html)
                        if dept_match:
                            user_info['department'] = dept_match.group(1).strip()

                        major_pattern = r'<span class="key add-title">专业班级</span><span class="value">([^<]+)</span>'
                        major_match = re.search(major_pattern, html)
                        if major_match:
                            user_info['major_class'] = major_match.group(1).strip()

                        return user_info
            except Exception as e:
                logger.error(f"获取用户信息失败: {e}")
            return {}

        async def get_course_schedule(self, student_id: str, cookies: Dict, week: int = None) -> Dict[str, Any]:
            """获取课表"""
            try:
                base_url = self.jwc_config.get("base_url", "https://nimt.jw.chaoxing.com")

                # 如果未指定周次，获取当前周
                if week is None:
                    week = await self.get_current_week(cookies)

                url = f"{base_url}/admin/getXsdSykb"
                data = {
                    'type': 1,  # 主修课程
                    'zc': week
                }

                async with aiohttp.ClientSession(cookies=cookies) as session:
                    async with session.post(url, data=data) as response:
                        if response.status == 200:
                            result = await response.json()
                            if result.get('ret') == 0:
                                return {
                                    "success": True,
                                    "week": week,
                                    "data": result.get('data', {})
                                }
                            else:
                                return {
                                    "success": False,
                                    "error": result.get('msg', '获取课表失败')
                                }
                        else:
                            return {
                                "success": False,
                                "error": f"请求失败，状态码: {response.status}"
                            }
            except Exception as e:
                logger.error(f"获取课表失败: {e}")
                return {"success": False, "error": f"获取课表失败: {str(e)}"}

        async def get_current_week(self, cookies: Dict) -> int:
            """获取当前周次"""
            try:
                base_url = self.jwc_config.get("base_url", "https://nimt.jw.chaoxing.com")
                url = f"{base_url}/admin/getCurrentPkZc"

                async with aiohttp.ClientSession(cookies=cookies) as session:
                    async with session.get(url) as response:
                        if response.status == 200:
                            result = await response.json()
                            if result.get('ret') == 0 and result.get('data'):
                                return result['data'][0]
            except Exception as e:
                logger.error(f"获取当前周次失败: {e}")

            # 默认返回第1周
            return 1

        async def check_all_courses_task(self):
            """检查所有用户的课程变动"""
            try:
                with self.get_db_connection() as conn:
                    cursor = conn.cursor()
                    cursor.execute("SELECT student_id, cookie FROM user_bindings WHERE status = 'active'")
                    users = cursor.fetchall()

                for student_id, cookie_json in users:
                    try:
                        cookies = json.loads(cookie_json) if cookie_json else {}
                        await self.check_course_changes(student_id, cookies)
                    except Exception as e:
                        logger.error(f"检查用户 {student_id} 课程变动失败: {e}")

            except Exception as e:
                logger.error(f"检查课程变动任务失败: {e}")

        async def check_course_changes(self, student_id: str, cookies: Dict):
            """检查课程变动"""
            try:
                # 获取当前课表
                current_course = await self.get_course_schedule(student_id, cookies)
                if not current_course.get("success"):
                    return

                week = current_course.get("week", 1)
                current_data = json.dumps(current_course.get("data", {}), ensure_ascii=False)

                with self.get_db_connection() as conn:
                    cursor = conn.cursor()

                    # 检查缓存
                    cursor.execute(
                        "SELECT course_data FROM course_cache WHERE student_id = ? AND week = ?",
                        (student_id, week)
                    )

                    cached = cursor.fetchone()
                    if cached:
                        old_data = cached[0]
                        if old_data != current_data:
                            # 检测到变动，记录并推送
                            await self.record_and_push_change(
                                student_id, week, old_data, current_data
                            )

                    # 更新缓存
                    cursor.execute(
                        """
                        INSERT OR REPLACE INTO course_cache (student_id, week, course_data)
                        VALUES (?, ?, ?)
                        """,
                        (student_id, week, current_data)
                    )
                    conn.commit()

            except Exception as e:
                logger.error(f"检查课程变动失败: {e}")

        async def record_and_push_change(self, student_id: str, week: int,
                                         old_data: str, new_data: str):
            """记录并推送课程变动"""
            try:
                # 解析新旧数据，找出具体变动
                old_courses = json.loads(old_data).get('jcKcxx', [])
                new_courses = json.loads(new_data).get('jcKcxx', [])

                changes = []

                # 简单的变动检测（实际可根据需求更精细）
                if len(old_courses) != len(new_courses):
                    changes.append({
                        "type": "course_count",
                        "message": f"课程数量从 {len(old_courses)} 变为 {len(new_courses)}"
                    })

                # 记录变动
                with self.get_db_connection() as conn:
                    cursor = conn.cursor()

                    for change in changes:
                        cursor.execute(
                            """
                            INSERT INTO course_changes (student_id, change_type, course_name, new_value, change_time)
                            VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
                            """,
                            (student_id, change["type"], "课表变动", change["message"])
                        )

                    conn.commit()

                # 发送变动通知
                await self.send_course_change_notification(student_id, week, changes)

            except Exception as e:
                logger.error(f"记录课程变动失败: {e}")

        async def send_course_change_notification(self, student_id: str, week: int, changes: List[Dict]):
            """发送课程变动通知"""
            try:
                with self.get_db_connection() as conn:
                    cursor = conn.cursor()
                    cursor.execute(
                        "SELECT qq_id FROM user_bindings WHERE student_id = ?",
                        (student_id,)
                    )
                    user = cursor.fetchone()

                if user:
                    qq_id = user[0]

                    message = f"📚 课程变动通知（第{week}周）\n\n"
                    for change in changes:
                        message += f"🔔 {change['message']}\n"

                    message += f"\n📅 时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"

                    await self.context.send_message(f"private:{qq_id}", [Comp.Plain(message)])

            except Exception as e:
                logger.error(f"发送课程变动通知失败: {e}")

        # ==================== 命令处理 ====================

        @filter.command("查看通知", alias={"通知列表", "最新通知"})
        async def cmd_view_notices(self, event: AstrMessageEvent, count: int = 5):
            """查看最近的通知

            参数:
            count: 查看最近几条通知，默认为5条，最多20条
            """
            try:
                if count < 1:
                    count = 1
                if count > 20:
                    count = 20

                with self.get_db_connection() as conn:
                    cursor = conn.cursor()
                    cursor.execute(
                        """
                        SELECT title, publish_date, url, site_id
                        FROM notices 
                        ORDER BY publish_date DESC, created_at DESC 
                        LIMIT ?
                        """,
                        (count,)
                    )

                    notices = cursor.fetchall()

                if not notices:
                    yield event.plain_result("📭 暂无通知记录")
                    return

                # 使用HTML渲染更美观的显示
                html_template = """
                <div style="font-family: 'Microsoft YaHei', sans-serif; padding: 20px; background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); color: white;">
                    <h1 style="text-align: center; margin-bottom: 30px;">📢 最新通知</h1>
                    <div style="background: rgba(255, 255, 255, 0.1); border-radius: 10px; padding: 20px;">
                        {% for notice in notices %}
                        <div style="margin-bottom: 20px; padding-bottom: 20px; border-bottom: 1px solid rgba(255, 255, 255, 0.2);">
                            <h3 style="margin: 0 0 10px 0; color: #ffd700;">{{ loop.index }}. {{ notice.title[:50] }}{% if notice.title|length > 50 %}...{% endif %}</h3>
                            <div style="color: #e0e0e0; font-size: 14px;">
                                <span>📅 {{ notice.date }}</span>
                                <span style="margin-left: 15px;">🏫 {{ notice.site }}</span>
                            </div>
                            <div style="margin-top: 5px; font-size: 12px; color: #b0b0b0;">🔗 {{ notice.url[:50] }}...</div>
                        </div>
                        {% endfor %}
                    </div>
                    <div style="text-align: center; margin-top: 20px; font-size: 12px; color: #d0d0d0;">
                        共 {{ notices|length }} 条通知 | {{ current_time }}
                    </div>
                </div>
                """

                # 准备渲染数据
                render_data = {
                    "notices": [],
                    "current_time": datetime.now().strftime("%Y-%m-%d %H:%M")
                }

                for title, pub_date, url, site_id in notices:
                    site_name = next((s["name"] for s in self.sites_config if s["id"] == site_id), "未知网站")
                    render_data["notices"].append({
                        "title": title,
                        "date": pub_date,
                        "site": site_name,
                        "url": url
                    })

                # 渲染为图片
                image_url = await self.html_render(
                    html_template,
                    render_data,
                    options={
                        "full_page": True,
                        "type": "jpeg",
                        "quality": 90,
                        "omit_background": True
                    }
                )

                yield event.image_result(image_url)

            except Exception as e:
                logger.error(f"查看通知失败: {e}")
                yield event.plain_result(f"❌ 查询失败: {str(e)}")

        @filter.command_group("nimt", alias={"南机电", "南京机电"})
        def nimt_group(self):
            """南京机电职业技术学院相关功能"""
            pass

        @nimt_group.command("网站列表")
        async def cmd_list_sites(self, event: AstrMessageEvent):
            """查看监控的网站列表"""
            try:
                if not self.sites_config:
                    yield event.plain_result("📭 暂无监控网站")
                    return

                enabled_count = sum(1 for site in self.sites_config if site.get("enabled", True))
                disabled_count = len(self.sites_config) - enabled_count

                html_template = """
                <div style="font-family: 'Microsoft YaHei', sans-serif; padding: 20px; background: linear-gradient(135deg, #74b9ff 0%, #0984e3 100%); color: white;">
                    <h1 style="text-align: center; margin-bottom: 30px;">🌐 监控网站列表</h1>
                    <div style="display: flex; justify-content: center; gap: 20px; margin-bottom: 20px;">
                        <div style="background: rgba(255, 255, 255, 0.2); padding: 10px 20px; border-radius: 20px; text-align: center;">
                            <div style="font-size: 24px; font-weight: bold;">{{ enabled_count }}</div>
                            <div style="font-size: 12px;">已启用</div>
                        </div>
                        <div style="background: rgba(255, 255, 255, 0.2); padding: 10px 20px; border-radius: 20px; text-align: center;">
                            <div style="font-size: 24px; font-weight: bold;">{{ disabled_count }}</div>
                            <div style="font-size: 12px;">已禁用</div>
                        </div>
                    </div>
                    <div style="background: rgba(255, 255, 255, 0.1); border-radius: 10px; padding: 20px;">
                        {% for site in sites %}
                        <div style="margin-bottom: 15px; padding: 15px; background: rgba(255, 255, 255, 0.05); border-radius: 8px;">
                            <div style="display: flex; justify-content: space-between; align-items: center;">
                                <div>
                                    <h3 style="margin: 0 0 5px 0; color: {% if site.enabled %}#55efc4{% else %}#fd79a8{% endif %};">
                                        {{ loop.index }}. {% if site.enabled %}✅{% else %}⛔{% endif %} {{ site.name }}
                                    </h3>
                                    <div style="font-size: 12px; color: #dfe6e9;">{{ site.remark }}</div>
                                </div>
                                <div style="font-size: 12px; background: rgba(255, 255, 255, 0.1); padding: 5px 10px; border-radius: 15px;">
                                    {{ site.id }}
                                </div>
                            </div>
                            <div style="margin-top: 10px; font-size: 12px; color: #b2bec3; word-break: break-all;">
                                🔗 {{ site.url }}
                            </div>
                        </div>
                        {% endfor %}
                    </div>
                    <div style="text-align: center; margin-top: 20px; font-size: 12px; color: #d0d0d0;">
                        检查间隔: {{ check_interval }}秒 | 最后更新: {{ current_time }}
                    </div>
                </div>
                """

                render_data = {
                    "sites": self.sites_config,
                    "enabled_count": enabled_count,
                    "disabled_count": disabled_count,
                    "check_interval": self.check_interval,
                    "current_time": datetime.now().strftime("%Y-%m-%d %H:%M")
                }

                image_url = await self.html_render(
                    html_template,
                    render_data,
                    options={
                        "full_page": True,
                        "type": "jpeg",
                        "quality": 90
                    }
                )

                yield event.image_result(image_url)

            except Exception as e:
                logger.error(f"列出网站失败: {e}")
                yield event.plain_result(f"❌ 查询失败: {str(e)}")

        @nimt_group.command("检查通知")
        @filter.permission_type(filter.PermissionType.ADMIN)
        async def cmd_check_notices(self, event: AstrMessageEvent):
            """立即检查新通知（管理员专用）"""
            yield event.plain_result("⏳ 开始检查新通知，请稍候...")

            try:
                new_notices = await self.check_all_sites()

                if new_notices:
                    # 发送简洁的文本通知，详细内容通过推送发送
                    response = f"✅ 发现 {len(new_notices)} 条新通知\n\n"
                    for i, notice in enumerate(new_notices[:3], 1):
                        response += f"{i}. {notice['title'][:30]}...\n"

                    if len(new_notices) > 3:
                        response += f"... 还有 {len(new_notices) - 3} 条\n"

                    response += "\n正在推送通知..."
                    yield event.plain_result(response)

                    # 推送详细通知
                    for notice in new_notices:
                        await self.send_notice_push(notice)

                    yield event.plain_result("✅ 推送完成")
                else:
                    yield event.plain_result("📭 未发现新通知")

            except Exception as e:
                logger.error(f"手动检查失败: {e}")
                yield event.plain_result(f"❌ 检查失败: {str(e)}")

        # ==================== 教务系统命令 ====================

        @filter.command("绑定教务", alias={"绑定学号", "绑定账号"})
        async def cmd_bind_jwc(self, event: AstrMessageEvent, student_id: str, password: str):
            """绑定教务系统账号

            参数:
            student_id: 学号
            password: 密码
            """
            qq_id = event.get_sender_id()

            # 检查是否已绑定
            with self.get_db_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT student_id FROM user_bindings WHERE qq_id = ?",
                    (qq_id,)
                )
                existing = cursor.fetchone()

                if existing:
                    yield event.plain_result(
                        f"⚠️ 您已经绑定了学号: {existing[0]}\n"
                        f"如需重新绑定，请先使用 /解绑教务"
                    )
                    return

            # 尝试登录验证
            yield event.plain_result("⏳ 正在验证账号密码，请稍候...")

            login_result = await self.login_jwc(student_id, password)

            if login_result.get("success"):
                try:
                    cookies = login_result.get("cookies", {})
                    user_info = login_result.get("user_info", {})

                    # 保存加密密码
                    encoded_password = base64.b64encode(password.encode()).decode()

                    with self.get_db_connection() as conn:
                        cursor = conn.cursor()

                        cursor.execute(
                            """
                            INSERT OR REPLACE INTO user_bindings 
                            (qq_id, student_id, password, name, class_name, cookie, last_login)
                            VALUES (?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                qq_id,
                                student_id,
                                encoded_password,
                                user_info.get('name', ''),
                                user_info.get('major_class', ''),
                                json.dumps(cookies),
                                datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                            )
                        )

                        conn.commit()

                    # 构建成功响应
                    response = f"✅ 绑定成功！\n\n"
                    response += f"👤 学号: {student_id}\n"

                    if user_info.get('name'):
                        response += f"📛 姓名: {user_info['name']}\n"
                    if user_info.get('department'):
                        response += f"🏫 院系: {user_info['department']}\n"
                    if user_info.get('major_class'):
                        response += f"🎓 班级: {user_info['major_class']}\n"

                    response += f"\n⏰ 绑定时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
                    response += f"\n\n✨ 现在可以使用以下功能："
                    response += f"\n• /我的课表 - 查询本周课表"
                    response += f"\n• /我的绑定 - 查看绑定信息"

                    yield event.plain_result(response)

                except Exception as e:
                    logger.error(f"保存绑定信息失败: {e}")
                    yield event.plain_result(f"❌ 登录成功但保存信息失败: {str(e)}")
            else:
                error_msg = login_result.get("error", "绑定失败")
                yield event.plain_result(
                    f"❌ {error_msg}\n\n"
                    f"可能的原因：\n"
                    f"1. 学号或密码错误\n"
                    f"2. 需要验证码（请稍后再试）\n"
                    f"3. 网络连接问题\n"
                    f"4. 教务系统维护中\n\n"
                    f"请检查后重试。"
                )

        @filter.command("解绑教务", alias={"解绑账号", "取消绑定"})
        async def cmd_unbind_jwc(self, event: AstrMessageEvent):
            """解绑教务系统账号"""
            qq_id = event.get_sender_id()

            try:
                with self.get_db_connection() as conn:
                    cursor = conn.cursor()

                    cursor.execute(
                        "SELECT student_id FROM user_bindings WHERE qq_id = ?",
                        (qq_id,)
                    )
                    existing = cursor.fetchone()

                    if not existing:
                        yield event.plain_result("📭 您尚未绑定教务系统")
                        return

                    student_id = existing[0]

                    cursor.execute(
                        "DELETE FROM user_bindings WHERE qq_id = ?",
                        (qq_id,)
                    )

                    # 同时清理课表缓存
                    cursor.execute(
                        "DELETE FROM course_cache WHERE student_id = ?",
                        (student_id,)
                    )

                    conn.commit()

                yield event.plain_result(f"✅ 解绑成功！已清除学号 {student_id} 的绑定信息。")

            except Exception as e:
                logger.error(f"解绑失败: {e}")
                yield event.plain_result(f"❌ 解绑失败: {str(e)}")

        @filter.command("我的绑定", alias={"绑定信息", "我的账号"})
        async def cmd_my_binding(self, event: AstrMessageEvent):
            """查看我的绑定信息"""
            qq_id = event.get_sender_id()

            try:
                with self.get_db_connection() as conn:
                    cursor = conn.cursor()

                    cursor.execute(
                        """
                        SELECT student_id, name, class_name, bind_time, last_login
                        FROM user_bindings WHERE qq_id = ?
                        """,
                        (qq_id,)
                    )

                    binding = cursor.fetchone()

                if not binding:
                    yield event.plain_result("📭 您尚未绑定教务系统")
                    return

                student_id, name, class_name, bind_time, last_login = binding

                # 使用HTML渲染美观的显示
                html_template = """
                <div style="font-family: 'Microsoft YaHei', sans-serif; padding: 20px; background: linear-gradient(135deg, #a29bfe 0%, #6c5ce7 100%); color: white;">
                    <h1 style="text-align: center; margin-bottom: 30px;">📋 绑定信息</h1>
                    <div style="background: rgba(255, 255, 255, 0.1); border-radius: 10px; padding: 20px;">
                        <div style="margin-bottom: 15px;">
                            <div style="font-size: 12px; color: #dfe6e9;">👤 QQ号</div>
                            <div style="font-size: 18px; font-weight: bold;">{{ qq_id }}</div>
                        </div>
                        <div style="margin-bottom: 15px;">
                            <div style="font-size: 12px; color: #dfe6e9;">🎓 学号</div>
                            <div style="font-size: 18px; font-weight: bold;">{{ student_id }}</div>
                        </div>
                        {% if name %}
                        <div style="margin-bottom: 15px;">
                            <div style="font-size: 12px; color: #dfe6e9;">📛 姓名</div>
                            <div style="font-size: 18px; font-weight: bold;">{{ name }}</div>
                        </div>
                        {% endif %}
                        {% if class_name %}
                        <div style="margin-bottom: 15px;">
                            <div style="font-size: 12px; color: #dfe6e9;">🏫 班级</div>
                            <div style="font-size: 18px; font-weight: bold;">{{ class_name }}</div>
                        </div>
                        {% endif %}
                        <div style="margin-bottom: 15px;">
                            <div style="font-size: 12px; color: #dfe6e9;">⏰ 绑定时间</div>
                            <div style="font-size: 16px;">{{ bind_time }}</div>
                        </div>
                        <div style="margin-bottom: 15px;">
                            <div style="font-size: 12px; color: #dfe6e9;">🔄 最后登录</div>
                            <div style="font-size: 16px;">{{ last_login }}</div>
                        </div>
                    </div>
                    <div style="text-align: center; margin-top: 20px; font-size: 12px; color: #d0d0d0;">
                        状态: ✅ 已绑定 | 查询时间: {{ current_time }}
                    </div>
                </div>
                """

                render_data = {
                    "qq_id": qq_id,
                    "student_id": student_id,
                    "name": name or "未获取",
                    "class_name": class_name or "未获取",
                    "bind_time": bind_time,
                    "last_login": last_login or "从未登录",
                    "current_time": datetime.now().strftime("%Y-%m-%d %H:%M")
                }

                image_url = await self.html_render(
                    html_template,
                    render_data,
                    options={
                        "full_page": True,
                        "type": "jpeg",
                        "quality": 90
                    }
                )

                yield event.image_result(image_url)

            except Exception as e:
                logger.error(f"查询绑定信息失败: {e}")
                yield event.plain_result(f"❌ 查询失败: {str(e)}")

        @filter.command("我的课表", alias={"课表查询", "查看课表"})
        async def cmd_my_course(self, event: AstrMessageEvent, week: int = None):
            """查询我的课表

            参数:
            week: 周次，默认为当前周
            """
            qq_id = event.get_sender_id()

            try:
                # 获取绑定信息
                with self.get_db_connection() as conn:
                    cursor = conn.cursor()
                    cursor.execute(
                        "SELECT student_id, cookie FROM user_bindings WHERE qq_id = ?",
                        (qq_id,)
                    )
                    binding = cursor.fetchone()

                if not binding:
                    yield event.plain_result("📭 请先绑定教务系统账号（使用 /绑定教务 学号 密码）")
                    return

                student_id, cookie_json = binding
                cookies = json.loads(cookie_json) if cookie_json else {}

                yield event.plain_result("⏳ 正在获取课表信息，请稍候...")

                # 获取课表
                course_result = await self.get_course_schedule(student_id, cookies, week)

                if not course_result.get("success"):
                    error_msg = course_result.get("error", "获取课表失败")
                    yield event.plain_result(f"❌ {error_msg}")
                    return

                course_data = course_result.get("data", {})
                current_week = course_result.get("week", 1)
                kb_info = course_data.get('jcKcxx', [])

                if not kb_info:
                    yield event.plain_result(f"📭 第{current_week}周暂无课程安排")
                    return

                # 按星期几整理课表
                schedule = {}
                weekdays = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]

                for day_index, day_courses in enumerate(kb_info):
                    kbxx = day_courses.get('kbxx', [])
                    for course_day in kbxx:
                        courses = course_day.get('kcxx', [])
                        for course in courses:
                            if course.get('kcmc') and course.get('kcmc') != '-':
                                day_name = weekdays[day_index % 7]
                                if day_name not in schedule:
                                    schedule[day_name] = []

                                schedule[day_name].append({
                                    "name": course.get('kcmc', ''),
                                    "teacher": course.get('teacher', ''),
                                    "classroom": course.get('classroom', ''),
                                    "time": f"{course.get('kssj', '')}-{course.get('jssj', '')}",
                                    "section": day_courses.get('jc', '')
                                })

                # 构建HTML模板
                html_template = """
                <div style="font-family: 'Microsoft YaHei', sans-serif; padding: 20px; background: linear-gradient(135deg, #81ecec 0%, #00cec9 100%); color: #2d3436;">
                    <h1 style="text-align: center; margin-bottom: 30px; color: #2d3436;">📅 我的课表</h1>
                    <div style="display: flex; justify-content: center; margin-bottom: 20px;">
                        <div style="background: rgba(45, 52, 54, 0.1); padding: 10px 20px; border-radius: 20px; text-align: center;">
                            <div style="font-size: 18px; font-weight: bold;">第 {{ week }} 周</div>
                            <div style="font-size: 12px; color: #636e72;">{{ current_time }}</div>
                        </div>
                    </div>
                    <div style="background: rgba(255, 255, 255, 0.8); border-radius: 10px; padding: 20px; box-shadow: 0 4px 6px rgba(0, 0, 0, 0.1);">
                        {% for day, courses in schedule.items() %}
                        <div style="margin-bottom: 25px;">
                            <div style="font-size: 16px; font-weight: bold; color: #0984e3; margin-bottom: 10px; padding-bottom: 5px; border-bottom: 2px solid #74b9ff;">
                                {{ day }}
                            </div>
                            {% if courses %}
                                {% for course in courses %}
                                <div style="margin-bottom: 15px; padding: 15px; background: rgba(116, 185, 255, 0.1); border-radius: 8px;">
                                    <div style="display: flex; justify-content: space-between; align-items: flex-start;">
                                        <div>
                                            <div style="font-size: 16px; font-weight: bold; color: #2d3436; margin-bottom: 5px;">
                                                {{ course.name }}
                                            </div>
                                            <div style="font-size: 12px; color: #636e72;">
                                                👨‍🏫 {{ course.teacher or '暂无' }} | 🏫 {{ course.classroom or '暂无' }}
                                            </div>
                                        </div>
                                        <div style="text-align: right;">
                                            <div style="font-size: 14px; font-weight: bold; color: #00b894;">
                                                第{{ course.section }}节
                                            </div>
                                            <div style="font-size: 12px; color: #636e72;">
                                                {{ course.time }}
                                            </div>
                                        </div>
                                    </div>
                                </div>
                                {% endfor %}
                            {% else %}
                                <div style="text-align: center; padding: 20px; color: #b2bec3; font-size: 14px;">
                                    无课程安排
                                </div>
                            {% endif %}
                        </div>
                        {% endfor %}
                    </div>
                    <div style="text-align: center; margin-top: 20px; font-size: 12px; color: #636e72;">
                        学号: {{ student_id }} | 共 {{ total_courses }} 门课程
                    </div>
                </div>
                """

                # 计算总课程数
                total_courses = sum(len(courses) for courses in schedule.values())

                render_data = {
                    "week": current_week,
                    "schedule": schedule,
                    "student_id": student_id,
                    "total_courses": total_courses,
                    "current_time": datetime.now().strftime("%Y-%m-%d %H:%M")
                }

                image_url = await self.html_render(
                    html_template,
                    render_data,
                    options={
                        "full_page": True,
                        "type": "jpeg",
                        "quality": 90,
                        "omit_background": True
                    }
                )

                yield event.image_result(image_url)

            except Exception as e:
                logger.error(f"查询课表失败: {e}")
                yield event.plain_result(f"❌ 查询失败: {str(e)}")

        @filter.command("测试登录", alias={"登录测试", "验证登录"})
        async def cmd_test_login(self, event: AstrMessageEvent, student_id: str, password: str):
            """测试教务系统登录

            参数:
            student_id: 学号
            password: 密码
            """
            yield event.plain_result("⏳ 正在测试登录，请稍候...")

            login_result = await self.login_jwc(student_id, password)

            if login_result.get("success"):
                cookies = login_result.get("cookies", {})
                user_info = login_result.get("user_info", {})

                response = f"✅ 登录成功！\n\n"
                response += f"👤 学号: {student_id}\n"

                if user_info.get('name'):
                    response += f"📛 姓名: {user_info['name']}\n"
                if user_info.get('department'):
                    response += f"🏫 院系: {user_info['department']}\n"
                if user_info.get('major_class'):
                    response += f"🎓 班级: {user_info['major_class']}\n"

                response += f"\n🔐 Cookies数量: {len(cookies)}个\n"
                response += f"💡 提示: {login_result.get('message', '登录成功')}"
                response += f"\n\n✨ 现在可以使用 /绑定教务 来绑定账号"

                yield event.plain_result(response)
            else:
                error_msg = login_result.get("error", "登录失败")
                yield event.plain_result(
                    f"❌ {error_msg}\n\n"
                    f"请检查：\n"
                    f"1. 学号和密码是否正确\n"
                    f"2. 网络连接是否正常\n"
                    f"3. 教务系统是否可访问"
                )

        @filter.command("帮助", alias={"help", "功能列表"})
        async def cmd_help(self, event: AstrMessageEvent):
            """显示插件帮助信息"""
            help_text = """
📚 南京机电职业技术学院通知监控插件 v2.1.0

🏫 通知监控功能：
• /查看通知 [数量] - 查看最新通知（默认5条）
• /nimt 网站列表 - 查看监控的网站列表
• /nimt 检查通知 - 立即检查新通知（管理员）

🎓 教务系统功能：
• /绑定教务 学号 密码 - 绑定教务系统账号
• /解绑教务 - 解绑教务系统账号
• /我的绑定 - 查看绑定信息
• /我的课表 [周次] - 查询课表（默认当前周）
• /测试登录 学号 密码 - 测试教务系统登录

⚙️ 其他命令：
• /帮助 - 显示此帮助信息

📝 使用提示：
1. 绑定账号后可以查询课表
2. 通知会自动推送到配置的用户/群组
3. 课表会以图片形式展示，更美观

🔧 管理员配置：
请通过AstrBot WebUI配置推送目标和监控网站

💡 如有问题，请联系插件开发者
            """

            yield event.plain_result(help_text)

        async def terminate(self):
            """插件卸载时的清理工作"""
            logger.info("南京机电通知监控插件正在卸载...")

            # 保存当前配置
            self.save_config()

            # 关闭数据库连接等清理工作
            logger.info("插件卸载完成")

else:
    print("南京机电通知监控插件无法加载:缺少必要的依赖或API")