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

# 首先检查必要的依赖
try:
    import aiohttp
    from bs4 import BeautifulSoup
    HAS_DEPENDENCIES = True
except ImportError as e:
    print(f"缺少依赖: {e}")
    HAS_DEPENDENCIES = False

# 检查AstrBot API
try:
    from astrbot.api.event import filter, AstrMessageEvent
    from astrbot.api.star import Context, Star, register
    from astrbot.api import logger
    HAS_ASTRBOT_API = True
except ImportError as e:
    print(f"AstrBot API导入失败: {e}")
    HAS_ASTRBOT_API = False

# 只在所有依赖都可用时才注册插件
if HAS_DEPENDENCIES and HAS_ASTRBOT_API:
    @register(
        "nimt_notice_monitor",
        "AstrBot",
        "南京机电职业技术学院通知监控插件",
        "2.0.4"
    )
    class NJIMTNoticeMonitor(Star):
        """南京机电通知监控插件"""

        def __init__(self, context: Context):
            super().__init__(context)

            # 获取插件数据目录
            self.data_dir = Path("data/plugin_data/nimt_notice_monitor")
            self.data_dir.mkdir(parents=True, exist_ok=True)

            # 数据库文件
            self.db_file = self.data_dir / "notices.db"

            # 配置文件
            self.config_file = self.data_dir / "config.json"

            # 加载配置
            self.config = self.load_config()
            self.sites_config = self.config.get("sites_config", [])
            self.push_targets = self.config.get("push_targets", {"users": [], "groups": []})
            self.check_interval = self.config.get("check_interval", 180)

            # 初始化数据库
            self.init_database()

            # 启动定时任务
            self.start_scheduler()

            logger.info("南京机电通知监控插件初始化完成")

        def load_config(self) -> Dict[str, Any]:
            """加载配置文件"""
            "sites_config": [
            # 主站
            {
                "id": "nimt_main",
                "name": "南京机电职业技术学院",
                "url": "https://www.nimt.edu.cn/739/list.htm",
                "base_url": "https://www.nimt.edu.cn",
                "remark": "学校主站通知公告",
                "enabled": True
            },
            # 教务处
            {
                "id": "jiaowu",
                "name": "教务处",
                "url": "https://www.nimt.edu.cn/jiaowu/396/list.htm",
                "base_url": "https://www.nimt.edu.cn",
                "remark": "教务处通知公告",
                "enabled": True
            },
            # 信息工程系
            {
                "id": "xinxi",
                "name": "信息工程系",
                "url": "https://www.nimt.edu.cn/xinxi/tzgg/list.htm",
                "base_url": "https://www.nimt.edu.cn",
                "remark": "信息工程系通知公告",
                "enabled": True
            },
            # 蓝岛创客空间
            {
                "id": "landao",
                "name": "蓝岛创客空间",
                "url": "https://www.nimt.edu.cn/landao/19517/list.htm",
                "base_url": "https://www.nimt.edu.cn",
                "remark": "蓝岛创客空间通知公告",
                "enabled": True
            },
            # 其他网站可以根据需要添加
            # {
            #     "id": "jixie",
            #     "name": "机械工程系",
            #     "url": "https://www.nimt.edu.cn/jixie/166/list.htm",
            #     "base_url": "https://www.nimt.edu.cn",
            #     "remark": "机械工程系通知公告",
            #     "enabled": True
            # },
            # {
            #     "id": "dianzi",
            #     "name": "电子工程系",
            #     "url": "https://www.nimt.edu.cn/dianzi/181/list.htm",
            #     "base_url": "https://www.nimt.edu.cn",
            #     "remark": "电子工程系通知公告",
            #     "enabled": True
            # },
            # {
            #     "id": "zidonghua",
            #     "name": "自动化工程系",
            #     "url": "https://www.nimt.edu.cn/zidonghua/204/list.htm",
            #     "base_url": "https://www.nimt.edu.cn",
            #     "remark": "自动化工程系通知公告",
            #     "enabled": True
            # }
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

            # 保存默认配置
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
                self.check_interval = config.get("check_interval", 180)
            except Exception as e:
                logger.error(f"保存配置文件失败: {e}")

        def init_database(self):
            """初始化数据库"""
            try:
                conn = sqlite3.connect(str(self.db_file))
                cursor = conn.cursor()

                # 创建通知记录表
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

                # 创建索引
                cursor.execute(
                    "CREATE INDEX IF NOT EXISTS idx_site_id ON notices(site_id)"
                )
                cursor.execute(
                    "CREATE INDEX IF NOT EXISTS idx_publish_date ON notices(publish_date)"
                )

                conn.commit()
                conn.close()
                logger.info("数据库初始化完成")
            except Exception as e:
                logger.error(f"数据库初始化失败: {e}")

        def start_scheduler(self):
            """启动定时任务"""
            try:
                # 尝试导入调度器
                from astrbot.utils.schedule import scheduler

                # 添加新任务
                @scheduler.scheduled_job('interval', minutes=self.check_interval, id='nimt_check_notices')
                async def scheduled_check():
                    await self.check_all_sites_task()

            except ImportError:
                logger.warning("未找到调度器，定时任务功能不可用")
            except Exception as e:
                logger.error(f"启动调度器失败: {e}")

        async def check_all_sites_task(self):
            """定时检查任务"""
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
            """获取页面内容"""
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "zh-CN,zh;q=0.9",
            }

            try:
                async with aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(total=10)
                ) as session:
                    async with session.get(url, headers=headers) as response:
                        response.raise_for_status()
                        return await response.text(encoding='utf-8')
            except aiohttp.ClientError as e:
                logger.error(f"请求失败 {url}: {e}")
            except Exception as e:
                logger.error(f"未知错误 {url}: {e}")

            return ""

        def parse_notices(self, html: str, site_config: Dict[str, Any]) -> List[Dict[str, Any]]:
            """解析通知列表"""
            if not html:
                return []

            try:
                soup = BeautifulSoup(html, 'html.parser')
                notices = []

                # 查找通知列表
                list_container = None

                # 尝试多种选择器
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
                    # 尝试直接查找包含news类的列表项
                    news_items = soup.find_all('li', class_=re.compile('news'))
                    if news_items:
                        list_container = soup.new_tag('div')
                        for item in news_items:
                            list_container.append(item)
                    else:
                        logger.warning(f"未找到通知列表容器: {site_config['name']}")
                        return notices

                # 解析每个通知项
                items = list_container.find_all('li')
                for item in items:
                    try:
                        # 查找标题链接
                        title_elem = item.find('a')
                        if not title_elem:
                            continue

                        title = title_elem.get_text(strip=True)
                        if not title:
                            continue

                        # 处理URL
                        relative_url = title_elem.get('href', '')
                        if relative_url.startswith('http'):
                            url = relative_url
                        elif relative_url.startswith('/'):
                            url = site_config["base_url"] + relative_url
                        else:
                            url = f"{site_config['base_url']}/{relative_url}"

                        # 提取日期
                        publish_date = datetime.now().strftime("%Y-%m-%d")

                        # 尝试查找日期元素
                        date_elems = item.find_all(['span', 'div', 'td'])
                        for elem in date_elems:
                            text = elem.get_text(strip=True)
                            # 匹配日期格式
                            date_match = re.search(r'(\d{4}[-/年]\d{1,2}[-/月]\d{1,2}[日]?)', text)
                            if date_match:
                                date_str = date_match.group(1)
                                # 清理日期格式
                                date_str = re.sub(r'[年月]', '-', date_str)
                                date_str = re.sub(r'[日]', '', date_str)
                                date_str = re.sub(r'/', '-', date_str)
                                publish_date = date_str
                                break

                        # 生成唯一ID
                        notice_id = hashlib.md5(
                            f"{site_config['id']}_{url}".encode()
                        ).hexdigest()

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
            """检查单个网站的通知"""
            new_notices = []

            try:
                html = await self.fetch_page(site_config["url"])
                notices = self.parse_notices(html, site_config)

                # 检查是否有新通知
                conn = sqlite3.connect(str(self.db_file))
                cursor = conn.cursor()

                for notice in notices:
                    cursor.execute(
                        "SELECT id FROM notices WHERE id = ?",
                        (notice["id"],)
                    )

                    if not cursor.fetchone():
                        # 插入新通知
                        cursor.execute(
                            """
                            INSERT INTO notices (id, site_id, title, url, publish_date)
                            VALUES (?, ?, ?, ?, ?)
                            """,
                            (
                                notice["id"],
                                notice["site_id"],
                                notice["title"],
                                notice["url"],
                                notice["publish_date"]
                            )
                        )
                        new_notices.append(notice)

                conn.commit()
                conn.close()

            except Exception as e:
                logger.error(f"检查网站 {site_config['name']} 失败: {e}")

            return new_notices

        async def check_all_sites(self) -> List[Dict[str, Any]]:
            """检查所有启用的网站"""
            all_new_notices = []

            for site in self.sites_config:
                if site.get("enabled", True):
                    try:
                        new_notices = await self.check_site_notices(site)
                        all_new_notices.extend(new_notices)

                        # 避免请求过快
                        await asyncio.sleep(1)

                    except Exception as e:
                        logger.error(f"检查 {site['name']} 失败: {e}")

            return all_new_notices

        async def send_notice_push(self, notice: Dict[str, Any]):
            """发送通知推送"""
            try:
                # 构建推送消息
                message = f"📢 新通知提醒\n\n"

                if notice.get("remark"):
                    message += f"📝 {notice['remark']}\n"

                message += f"🏫 {notice['site_name']}\n"
                message += f"📌 {notice['title']}\n"
                message += f"📅 {notice['publish_date']}\n"
                message += f"🔗 {notice['url']}\n"

                # 推送给所有用户
                for user_id in self.push_targets["users"]:
                    try:
                        await self.context.send_message(
                            f"private:{user_id}",
                            message
                        )
                    except Exception as e:
                        logger.error(f"推送用户 {user_id} 失败: {e}")

                # 推送给所有群组
                for group_id in self.push_targets["groups"]:
                    try:
                        await self.context.send_message(
                            f"group:{group_id}",
                            message
                        )
                    except Exception as e:
                        logger.error(f"推送群组 {group_id} 失败: {e}")

                # 标记为已推送
                conn = sqlite3.connect(str(self.db_file))
                cursor = conn.cursor()
                cursor.execute(
                    """
                    UPDATE notices 
                    SET notified = 1, notified_at = CURRENT_TIMESTAMP 
                    WHERE id = ?
                    """,
                    (notice["id"],)
                )
                conn.commit()
                conn.close()

            except Exception as e:
                logger.error(f"发送推送失败: {e}")

        @filter.command("查看通知")
        async def cmd_view_notices(self, event: AstrMessageEvent):
            """查看最近3天的通知"""
            try:
                # 计算3天前的日期
                three_days_ago = (datetime.now() - timedelta(days=3)).strftime("%Y-%m-%d")

                conn = sqlite3.connect(str(self.db_file))
                cursor = conn.cursor()

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

        # 修正命令组定义
        @filter.command_group("nimt")
        def nimt_group(self):
            """南京机电通知监控插件管理命令组"""
            pass

        @nimt_group.command("添加网站")
        @filter.permission_type(filter.PermissionType.ADMIN)
        async def cmd_add_site(self, event: AstrMessageEvent, site_id: str, name: str, url: str, base_url: str, *remark_parts):
            """添加监控网站

            参数:
            site_id: 网站唯一标识
            name: 网站名称
            url: 通知列表URL
            base_url: 基础URL（用于拼接相对链接）
            remark: 备注信息（可选）
            """
            try:
                # 合并备注信息
                remark = ' '.join(remark_parts) if remark_parts else ""

                # 检查是否已存在
                for site in self.sites_config:
                    if site["id"] == site_id:
                        yield event.plain_result(f"网站ID '{site_id}' 已存在")
                        return

                # 添加新网站配置
                new_site = {
                    "id": site_id,
                    "name": name,
                    "url": url,
                    "base_url": base_url,
                    "remark": remark,
                    "enabled": True
                }

                self.sites_config.append(new_site)
                self.config["sites_config"] = self.sites_config
                self.save_config()

                yield event.plain_result(f"✅ 已添加网站：{name}")

            except Exception as e:
                logger.error(f"添加网站失败: {e}")
                yield event.plain_result(f"添加失败: {str(e)}")

        @nimt_group.command("删除网站")
        @filter.permission_type(filter.PermissionType.ADMIN)
        async def cmd_delete_site(self, event: AstrMessageEvent, site_id: str):
            """删除监控网站

            参数:
            site_id: 网站唯一标识
            """
            try:
                # 查找并删除
                new_config = [s for s in self.sites_config if s["id"] != site_id]

                if len(new_config) == len(self.sites_config):
                    yield event.plain_result(f"未找到网站ID '{site_id}'")
                    return

                self.sites_config = new_config
                self.config["sites_config"] = self.sites_config
                self.save_config()

                yield event.plain_result(f"✅ 已删除网站：{site_id}")

            except Exception as e:
                logger.error(f"删除网站失败: {e}")
                yield event.plain_result(f"删除失败: {str(e)}")

        @nimt_group.command("网站列表")
        async def cmd_list_sites(self, event: AstrMessageEvent):
            """查看所有监控网站"""
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

        @nimt_group.command("添加推送用户")
        @filter.permission_type(filter.PermissionType.ADMIN)
        async def cmd_add_push_user(self, event: AstrMessageEvent, user_id: str):
            """添加推送用户

            参数:
            user_id: 用户ID
            """
            try:
                if user_id not in self.push_targets["users"]:
                    self.push_targets["users"].append(user_id)
                    self.config["push_targets"] = self.push_targets
                    self.save_config()
                    yield event.plain_result(f"✅ 已添加推送用户：{user_id}")
                else:
                    yield event.plain_result("⚠️ 该用户已在推送列表中")

            except Exception as e:
                logger.error(f"添加推送用户失败: {e}")
                yield event.plain_result(f"添加失败: {str(e)}")

        @nimt_group.command("添加推送群组")
        @filter.permission_type(filter.PermissionType.ADMIN)
        async def cmd_add_push_group(self, event: AstrMessageEvent, group_id: str):
            """添加推送群组

            参数:
            group_id: 群组ID
            """
            try:
                if group_id not in self.push_targets["groups"]:
                    self.push_targets["groups"].append(group_id)
                    self.config["push_targets"] = self.push_targets
                    self.save_config()
                    yield event.plain_result(f"✅ 已添加推送群组：{group_id}")
                else:
                    yield event.plain_result("⚠️ 该群组已在推送列表中")

            except Exception as e:
                logger.error(f"添加推送群组失败: {e}")
                yield event.plain_result(f"添加失败: {str(e)}")

        @nimt_group.command("推送列表")
        @filter.permission_type(filter.PermissionType.ADMIN)
        async def cmd_list_push_targets(self, event: AstrMessageEvent):
            """查看推送目标"""
            try:
                response = "📢 推送目标列表\n\n"

                response += "👤 用户列表：\n"
                if self.push_targets["users"]:
                    for user_id in self.push_targets["users"]:
                        response += f"  - {user_id}\n"
                else:
                    response += "  暂无推送用户\n"

                response += "\n👥 群组列表：\n"
                if self.push_targets["groups"]:
                    for group_id in self.push_targets["groups"]:
                        response += f"  - {group_id}\n"
                else:
                    response += "  暂无推送群组\n"

                yield event.plain_result(response)

            except Exception as e:
                logger.error(f"列出推送目标失败: {e}")
                yield event.plain_result(f"查询失败: {str(e)}")

        @nimt_group.command("检查通知")
        @filter.permission_type(filter.PermissionType.ADMIN)
        async def cmd_check_notices(self, event: AstrMessageEvent):
            """手动检查新通知"""
            yield event.plain_result("开始检查新通知，请稍候...")

            try:
                new_notices = await self.check_all_sites()

                if new_notices:
                    response = f"✅ 发现 {len(new_notices)} 条新通知：\n\n"
                    for notice in new_notices[:5]:  # 最多显示5条
                        response += f"📌 {notice['title']}\n"
                        response += f"   📅 {notice['publish_date']}\n"
                        response += f"   🏫 {notice['site_name']}\n\n"

                    if len(new_notices) > 5:
                        response += f"... 还有 {len(new_notices) - 5} 条未显示\n"

                    response += "正在推送..."
                    yield event.plain_result(response)

                    # 推送新通知
                    for notice in new_notices:
                        await self.send_notice_push(notice)

                    yield event.plain_result("✅ 推送完成")
                else:
                    yield event.plain_result("未发现新通知")

            except Exception as e:
                logger.error(f"手动检查失败: {e}")
                yield event.plain_result(f"检查失败: {str(e)}")

        @nimt_group.command("通知统计")
        @filter.permission_type(filter.PermissionType.ADMIN)
        async def cmd_notice_stats(self, event: AstrMessageEvent):
            """查看通知统计"""
            try:
                conn = sqlite3.connect(str(self.db_file))
                cursor = conn.cursor()

                # 总通知数
                cursor.execute("SELECT COUNT(*) FROM notices")
                total = cursor.fetchone()[0]

                # 今日通知数
                today = datetime.now().strftime("%Y-%m-%d")
                cursor.execute(
                    "SELECT COUNT(*) FROM notices WHERE publish_date = ?",
                    (today,)
                )
                today_count = cursor.fetchone()[0]

                # 各网站通知数
                cursor.execute("""
                    SELECT site_id, COUNT(*) as count 
                    FROM notices 
                    GROUP BY site_id
                """)
                site_stats = cursor.fetchall()

                conn.close()

                response = "📊 通知统计\n\n"
                response += f"📈 总通知数：{total} 条\n"
                response += f"📅 今日通知：{today_count} 条\n\n"

                response += "🏫 各网站统计：\n"
                for site_id, count in site_stats:
                    # 查找网站名称
                    site_name = site_id
                    for site in self.sites_config:
                        if site["id"] == site_id:
                            site_name = site["name"]
                            break

                    response += f"  {site_name}: {count} 条\n"

                yield event.plain_result(response)

            except Exception as e:
                logger.error(f"查询统计失败: {e}")
                yield event.plain_result(f"查询失败: {str(e)}")

        @nimt_group.command("手动推送")
        @filter.permission_type(filter.PermissionType.ADMIN)
        async def cmd_manual_push(self, event: AstrMessageEvent, days: int = 1):
            """手动推送最近N天的通知

            参数:
            days: 推送最近几天的通知（默认1天）
            """
            try:
                # 计算日期
                target_date = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")

                conn = sqlite3.connect(str(self.db_file))
                cursor = conn.cursor()

                # 查找未推送的通知
                cursor.execute(
                    """
                    SELECT id, site_id, title, url, publish_date 
                    FROM notices 
                    WHERE publish_date >= ? AND notified = 0
                    ORDER BY publish_date DESC
                    LIMIT 10
                    """,
                    (target_date,)
                )

                notices = cursor.fetchall()
                conn.close()

                if not notices:
                    yield event.plain_result(f"最近{days}天没有未推送的通知")
                    return

                response = f"📤 开始推送最近{days}天的通知...\n"
                response += f"找到 {len(notices)} 条未推送通知\n\n"

                # 推送通知
                count = 0
                for notice_id, site_id, title, url, pub_date in notices:
                    # 查找网站信息
                    site_name = site_id
                    remark = ""
                    for site in self.sites_config:
                        if site["id"] == site_id:
                            site_name = site["name"]
                            remark = site.get("remark", "")
                            break

                    notice = {
                        "id": notice_id,
                        "title": title,
                        "publish_date": pub_date,
                        "url": url,
                        "site_name": site_name,
                        "remark": remark
                    }
                    await self.send_notice_push(notice)
                    count += 1

                yield event.plain_result(f"✅ 已推送 {count} 条通知")

            except Exception as e:
                logger.error(f"手动推送失败: {e}")
                yield event.plain_result(f"推送失败: {str(e)}")

        @nimt_group.command("清空数据")
        @filter.permission_type(filter.PermissionType.ADMIN)
        async def cmd_clear_data(self, event: AstrMessageEvent):
            """清空通知数据库"""
            try:
                conn = sqlite3.connect(str(self.db_file))
                cursor = conn.cursor()

                cursor.execute("DELETE FROM notices")

                conn.commit()
                conn.close()

                yield event.plain_result("✅ 已清空通知数据库")

            except Exception as e:
                logger.error(f"清空数据失败: {e}")
                yield event.plain_result(f"清空失败: {str(e)}")

        async def terminate(self):
            """插件卸载时调用"""
            logger.info("南京机电通知监控插件正在卸载...")
            # 清理资源
            pass

else:
    # 如果缺少依赖，打印错误信息
    print("南京机电通知监控插件无法加载：缺少必要的依赖或API")
    if not HAS_DEPENDENCIES:
        print("请安装依赖：pip install aiohttp beautifulsoup4")
    if not HAS_ASTRBOT_API:
        print("请检查AstrBot版本和API兼容性")
