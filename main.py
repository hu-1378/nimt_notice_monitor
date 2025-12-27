"""
南京机电职业技术学院通知监控插件
监控学校官网及二级学院网站的通知公告，自动推送新通知
"""
import json
import hashlib
import asyncio
import re
import sqlite3
from datetime import datetime, timedelta
from typing import List, Dict, Any
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
        "2.0.8"
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
                "check_interval": 180
            }

            if self.config_file.exists():
                try:
                    with open(self.config_file, 'r', encoding='utf-8') as f:
                        return json.load(f)
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
            except Exception as e:
                logger.error(f"保存配置文件失败: {e}")

        def init_database(self):
            try:
                conn = sqlite3.connect(str(self.db_file))
                cursor = conn.cursor()

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

                conn.commit()
                conn.close()
                logger.info("数据库初始化完成")
            except Exception as e:
                logger.error(f"数据库初始化失败: {e}")

        def start_scheduler(self):
            try:
                from astrbot.utils.schedule import scheduler

                @scheduler.scheduled_job('interval', minutes=self.check_interval, id='nimt_check_notices')
                async def scheduled_check():
                    await self.check_all_sites_task()

            except ImportError:
                logger.warning("未找到调度器，定时任务功能不可用")
            except Exception as e:
                logger.error(f"启动调度器失败: {e}")

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
                    # 查看最近3天的通知（原有功能）
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

        async def terminate(self):
            logger.info("南京机电通知监控插件正在卸载...")

else:
    print("南京机电通知监控插件无法加载：缺少必要的依赖或API")