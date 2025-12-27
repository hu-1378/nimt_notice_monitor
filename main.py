"""
南京机电职业技术学院通知监控插件
监控学校官网及二级学院网站的通知公告，自动推送新通知
"""
import json
import hashlib
import asyncio
import re
from datetime import datetime, timedelta
from typing import List, Dict, Any, Optional
from pathlib import Path

import aiohttp
from bs4 import BeautifulSoup
from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult
from astrbot.api.star import Context, Star, register
from astrbot.api import logger
from astrbot.api.config import AstrBotConfig


@register(
    "nimt_notice_monitor",
    "AstrBot",
    "南京机电职业技术学院通知监控插件",
    "2.0.0"
)
class NJIMTNoticeMonitor(Star):
    """南京机电通知监控插件"""

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config

        # 数据存储路径
        self.data_dir = Path("data/plugin_data/nimt_notice_monitor")
        self.data_dir.mkdir(parents=True, exist_ok=True)

        # 数据库文件
        self.db_file = self.data_dir / "notices.db"

        # 初始化配置
        self.sites_config = self.load_config("sites_config")
        self.push_targets = self.load_config("push_targets")
        self.check_interval = self.config.get("check_interval", 180)

        # 初始化数据库
        self.init_database()

        # 启动定时任务
        self.start_scheduler()

        logger.info("南京机电通知监控插件初始化完成")

    def load_config(self, key: str) -> Any:
        """加载配置"""
        try:
            config_str = self.config.get(key, "")
            if config_str:
                return json.loads(config_str)
        except json.JSONDecodeError as e:
            logger.error(f"配置解析失败 {key}: {e}")

        # 返回默认值
        defaults = {
            "sites_config": [
                {
                    "id": "nimt_main",
                    "name": "南京机电职业技术学院",
                    "url": "http://www.nimt.edu.cn/739/list.htm",
                    "base_url": "http://www.nimt.edu.cn",
                    "remark": "学校主站通知公告",
                    "enabled": True
                }
            ],
            "push_targets": {
                "users": [],
                "groups": []
            }
        }
        return defaults.get(key, {})

    def save_config(self, key: str, value: Any):
        """保存配置"""
        try:
            self.config[key] = json.dumps(value, ensure_ascii=False, indent=2)
            self.config.save_config()
        except Exception as e:
            logger.error(f"保存配置失败 {key}: {e}")

    def init_database(self):
        """初始化数据库"""
        import sqlite3

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
        from astrbot.utils.schedule import scheduler

        # 移除可能存在的旧任务
        try:
            scheduler.remove_job('nimt_check_notices')
        except:
            pass

        # 添加新任务
        @scheduler.scheduled_job('interval', minutes=self.check_interval, id='nimt_check_notices')
        async def scheduled_check():
            await self.check_all_sites_task()

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

            # 尝试多种选择器
            list_selectors = [
                'ul.news_list',
                'ul.wp_list',
                'div.news_list ul',
                'div.list ul',
                'div.article-list ul'
            ]

            list_container = None
            for selector in list_selectors:
                list_container = soup.select_one(selector)
                if list_container:
                    break

            if not list_container:
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
                        date_match = re.search(r'(\d{4}[-/]\d{1,2}[-/]\d{1,2})', text)
                        if date_match:
                            publish_date = date_match.group(1)
                            publish_date = re.sub(r'[/]', '-', publish_date)
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
            import sqlite3
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
            import sqlite3
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

            import sqlite3
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

    @filter.command("添加网站")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def cmd_add_site(self, event: AstrMessageEvent, site_id: str, name: str, url: str, base_url: str,
                           *remark_parts):
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
            self.save_config("sites_config", self.sites_config)

            yield event.plain_result(f"✅ 已添加网站：{name}")

        except Exception as e:
            logger.error(f"添加网站失败: {e}")
            yield event.plain_result(f"添加失败: {str(e)}")

    @filter.command("删除网站")
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
            self.save_config("sites_config", self.sites_config)

            yield event.plain_result(f"✅ 已删除网站：{site_id}")

        except Exception as e:
            logger.error(f"删除网站失败: {e}")
            yield event.plain_result(f"删除失败: {str(e)}")

    @filter.command("网站列表")
    @filter.permission_type(filter.PermissionType.ADMIN)
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

    @filter.command("添加推送用户")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def cmd_add_push_user(self, event: AstrMessageEvent, user_id: str):
        """添加推送用户

        参数:
        user_id: 用户ID
        """
        try:
            if user_id not in self.push_targets["users"]:
                self.push_targets["users"].append(user_id)
                self.save_config("push_targets", self.push_targets)
                yield event.plain_result(f"✅ 已添加推送用户：{user_id}")
            else:
                yield event.plain_result("⚠️ 该用户已在推送列表中")

        except Exception as e:
            logger.error(f"添加推送用户失败: {e}")
            yield event.plain_result(f"添加失败: {str(e)}")

    @filter.command("添加推送群组")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def cmd_add_push_group(self, event: AstrMessageEvent, group_id: str):
        """添加推送群组

        参数:
        group_id: 群组ID
        """
        try:
            if group_id not in self.push_targets["groups"]:
                self.push_targets["groups"].append(group_id)
                self.save_config("push_targets", self.push_targets)
                yield event.plain_result(f"✅ 已添加推送群组：{group_id}")
            else:
                yield event.plain_result("⚠️ 该群组已在推送列表中")

        except Exception as e:
            logger.error(f"添加推送群组失败: {e}")
            yield event.plain_result(f"添加失败: {str(e)}")

    @filter.command("推送列表")
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

    @filter.command("检查通知")
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

    @filter.command("通知统计")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def cmd_notice_stats(self, event: AstrMessageEvent):
        """查看通知统计"""
        try:
            import sqlite3
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

    async def terminate(self):
        """插件卸载时调用"""
        logger.info("南京机电通知监控插件正在卸载...")
        # 清理资源
        pass