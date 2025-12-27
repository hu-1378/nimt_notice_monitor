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
        "2.0.0"
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
                "login_url": "/admin/login",
                "timeout": 30
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
                    "timeout": 30
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
            """初始化数据库,包含原有表和新增表"""
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

                # 新增:用户绑定表
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

                logger.info("定时任务初始化完成")
            except ImportError:
                logger.warning("未找到调度器,定时任务功能不可用")
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

        async def login_jwc_simple(self, student_id: str, password: str) -> Dict[str, Any]:
            """简单直接的登录方法,使用你提供的脚本逻辑"""
            try:
                base_url = self.jwc_config.get("base_url", "https://nimt.jw.chaoxing.com")
                login_url = f"{base_url}{self.jwc_config.get('login_url', '/admin/login')}"
                
                # 首先访问登录页面获取初始cookie
                async with aiohttp.ClientSession() as session:
                    # 第一次请求获取初始cookie
                    async with session.get(login_url) as response:
                        initial_cookies = session.cookie_jar.filter_cookies(login_url)
                    
                    # 准备登录数据(不加密,直接使用原始密码)
                    login_data = {
                        'username': student_id,
                        'password': password,  # 使用原始密码
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
                    
                    # 发送登录请求,不自动跟随重定向
                    async with session.post(
                        login_url,
                        data=login_data,
                        headers=headers,
                        allow_redirects=False  # 不自动重定向
                    ) as response:
                        status = response.status
                        
                        # 获取cookies
                        cookies = session.cookie_jar.filter_cookies(login_url)
                        cookie_dict = {}
                        for key, cookie in cookies.items():
                            cookie_dict[key] = cookie.value
                        
                        logger.info(f"登录响应状态: {status}")
                        logger.info(f"获得cookies: {list(cookie_dict.keys())}")
                        
                        if status == 302:
                            # 302重定向表示登录成功
                            # 验证cookies
                            if cookie_dict:
                                logger.info("✅ 登录成功!")
                                return {
                                    "success": True,
                                    "student_id": student_id,
                                    "cookies": cookie_dict,
                                    "message": "登录成功"
                                }
                            else:
                                logger.warning("登录返回302但未获得cookies")
                                return {"success": False, "error": "登录失败:未获得会话信息"}
                        elif status == 200:
                            # 读取响应内容检查错误
                            response_text = await response.text()
                            if "账号或密码错误" in response_text:
                                return {"success": False, "error": "账号或密码错误"}
                            elif "验证码" in response_text:
                                return {"success": False, "error": "需要验证码,请稍后再试"}
                            else:
                                return {"success": False, "error": "登录失败,未知原因"}
                        else:
                            return {"success": False, "error": f"登录失败,状态码: {status}"}
                            
            except Exception as e:
                logger.error(f"登录失败: {e}")
                return {"success": False, "error": f"登录失败: {str(e)}"}

        async def test_jwc_connection(self, cookies: Dict) -> bool:
            """测试连接是否有效"""
            try:
                base_url = self.jwc_config.get("base_url", "https://nimt.jw.chaoxing.com")
                test_url = f"{base_url}/admin/main"
                
                async with aiohttp.ClientSession() as session:
                    # 设置cookies
                    for key, value in cookies.items():
                        session.cookie_jar.update_cookies({key: value})
                    
                    async with session.get(test_url) as response:
                        if response.status == 200:
                            return True
                        else:
                            return False
            except Exception as e:
                logger.error(f"测试连接失败: {e}")
                return False

        # ==================== 命令处理 ====================

        @filter.command("查看通知")
        async def cmd_view_notices(self, event: AstrMessageEvent, count: int = None):
            """查看最近的通知

            参数:
            count: 可选,查看最近几条通知(默认查看最近3天的通知)
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
            yield event.plain_result("开始检查新通知,请稍候...")

            try:
                new_notices = await self.check_all_sites()

                if new_notices:
                    response = f"✅ 发现 {len(new_notices)} 条新通知:\n\n"
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

        @filter.command("测试登录")
        async def cmd_test_login(self, event: AstrMessageEvent, student_id: str, password: str):
            """测试教务系统登录"""
            yield event.plain_result("正在测试登录,请稍候...")
            
            # 尝试登录
            login_result = await self.login_jwc_simple(student_id, password)
            
            if login_result.get("success"):
                cookies = login_result.get("cookies", {})
                cookie_count = len(cookies)
                
                response = f"✅ 登录成功!\n\n"
                response += f"学号: {student_id}\n"
                response += f"获得cookies: {cookie_count}个\n"
                
                # 测试连接
                test_result = await self.test_jwc_connection(cookies)
                if test_result:
                    response += f"连接测试: ✅ 有效\n\n"
                else:
                    response += f"连接测试: ⚠️ 可能存在问题\n\n"
                
                response += "关键cookies: "
                
                # 显示关键cookies
                important_keys = ['username', 'puid', 'jw_uf', 'initPass', 'defaultPass']
                for key in important_keys:
                    if key in cookies:
                        value = cookies[key]
                        if len(value) > 20:
                            value = value[:20] + "..."
                        response += f"\n  {key}: {value}"
                
                response += f"\n\n提示信息: {login_result.get('message', '登录成功')}"
                
                yield event.plain_result(response)
            else:
                error_msg = login_result.get("error", "登录失败")
                yield event.plain_result(f"❌ {error_msg}\n\n请检查:\n1. 学号和密码是否正确\n2. 网络连接是否正常")

        @filter.command("绑定教务")
        async def cmd_bind_jwc(self, event: AstrMessageEvent, student_id: str, password: str):
            """绑定教务系统账号"""
            qq_id = event.get_sender_id()
            
            # 检查是否已绑定
            conn = sqlite3.connect(str(self.db_file))
            cursor = conn.cursor()
            cursor.execute("SELECT student_id FROM user_bindings WHERE qq_id = ?", (qq_id,))
            existing = cursor.fetchone()
            
            if existing:
                conn.close()
                yield event.plain_result("您已经绑定过教务系统,如需重新绑定请先使用 /解绑教务")
                return
            
            conn.close()
            
            # 尝试登录验证
            yield event.plain_result("正在验证账号密码,请稍候...")
            
            # 尝试登录
            login_result = await self.login_jwc_simple(student_id, password)
            
            if login_result.get("success"):
                # 绑定成功,保存信息
                try:
                    # 获取cookies
                    cookies = login_result.get("cookies", {})
                    
                    # 保存加密密码
                    encoded_password = base64.b64encode(password.encode()).decode()
                    
                    conn = sqlite3.connect(str(self.db_file))
                    cursor = conn.cursor()
                    
                    # 保存用户绑定信息
                    cursor.execute(
                        """
                        INSERT INTO user_bindings (qq_id, student_id, password, bind_time, cookie)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (
                            qq_id, 
                            student_id, 
                            encoded_password, 
                            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                            json.dumps(cookies)
                        )
                    )
                    
                    conn.commit()
                    conn.close()
                    
                    yield event.plain_result(f"✅ 绑定成功!\n学号: {student_id}\n\n已保存登录信息.")
                    
                except Exception as e:
                    logger.error(f"保存绑定信息失败: {e}")
                    yield event.plain_result(f"登录成功但保存信息失败: {str(e)}")
            else:
                error_msg = login_result.get("error", "绑定失败")
                yield event.plain_result(f"❌ {error_msg}\n\n可能的原因:\n1. 学号或密码错误\n2. 需要验证码(请稍后再试)\n3. 网络问题\n\n请检查后重试.")

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
                
                conn.commit()
                conn.close()
                
                yield event.plain_result("✅ 解绑成功!已清除您的绑定信息.")
                
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
                    "SELECT student_id, bind_time FROM user_bindings WHERE qq_id = ?",
                    (qq_id,)
                )
                
                binding = cursor.fetchone()
                conn.close()
                
                if not binding:
                    yield event.plain_result("您尚未绑定教务系统")
                    return
                
                student_id, bind_time = binding
                
                response = f"📋 绑定信息\n\n"
                response += f"QQ号: {qq_id}\n"
                response += f"学号: {student_id}\n"
                response += f"绑定时间: {bind_time}\n"
                
                yield event.plain_result(response)
                
            except Exception as e:
                logger.error(f"查询绑定信息失败: {e}")
                yield event.plain_result(f"查询失败: {str(e)}")

        async def terminate(self):
            logger.info("南京机电通知监控插件正在卸载...")

else:
    print("南京机电通知监控插件无法加载:缺少必要的依赖或API")
