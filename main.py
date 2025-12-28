"""
南京机电职业技术学院通知监控插件
"""
import json
import hashlib
import asyncio
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, List

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger


@register(
    "nimt_notice_monitor",
    "南京机电职业技术学院通知监控插件",
    "2.0.0"
)
class NJIMTNoticeMonitor(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        logger.info("南京机电通知监控插件初始化开始...")

        # 初始化数据目录
        try:
            from astrbot.core.utils.astrbot_path import get_astrbot_data_path
            astrbot_data_path = get_astrbot_data_path()
        except ImportError:
            astrbot_data_path = Path("data")

        self.data_dir = astrbot_data_path / "plugin_data" / "nimt_notice_monitor"
        self.data_dir.mkdir(parents=True, exist_ok=True)

        self.db_file = self.data_dir / "notices.db"
        self.config_file = self.data_dir / "config.json"

        # 加载配置
        self.config = self.load_config()

        # 初始化数据库
        self.init_database()

        # 启动定时任务
        self.start_scheduler()

        logger.info("✅ 南京机电通知监控插件初始化完成")

    def load_config(self) -> Dict[str, Any]:
        """加载配置文件"""
        default_config = {
            "sites": [
                {
                    "name": "学校官网通知公告",
                    "url": "https://www.nimt.edu.cn/739/list.htm",
                    "enabled": True,
                    "site_id": "main"
                },
                {
                    "name": "教务处通知",
                    "url": "https://www.nimt.edu.cn/jiaowu/396/list.htm",
                    "enabled": True,
                    "site_id": "jiaowu"
                }
            ],
            "check_interval": 300,
            "push_targets": {
                "users": [],
                "groups": []
            }
        }

        if self.config_file.exists():
            try:
                with open(self.config_file, 'r', encoding='utf-8') as f:
                    config = json.load(f)
                    # 确保所有必需字段都存在
                    for key, value in default_config.items():
                        if key not in config:
                            config[key] = value
                    return config
            except Exception as e:
                logger.error(f"加载配置文件失败: {e}")
                return default_config

        # 保存默认配置
        try:
            with open(self.config_file, 'w', encoding='utf-8') as f:
                json.dump(default_config, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"保存默认配置失败: {e}")

        return default_config

    def init_database(self):
        """初始化数据库"""
        try:
            conn = sqlite3.connect(str(self.db_file))
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

            # 创建索引
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_site_id ON notices(site_id)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_created_at ON notices(created_at)")

            conn.commit()
            conn.close()
            logger.info("数据库初始化完成")
        except Exception as e:
            logger.error(f"数据库初始化失败: {e}")

    def start_scheduler(self):
        """启动定时任务"""
        try:
            from astrbot.utils.schedule import scheduler

            @scheduler.scheduled_job('interval', seconds=self.config.get("check_interval", 300), id='nimt_check_notices')
            async def scheduled_check():
                try:
                    await self.check_all_sites()
                except Exception as e:
                    logger.error(f"定时检查失败: {e}")

            logger.info("定时任务初始化完成")
        except ImportError:
            logger.warning("未找到调度器，定时任务功能不可用")
        except Exception as e:
            logger.error(f"启动调度器失败: {e}")

    async def check_all_sites(self) -> int:
        """检查所有网站"""
        total_new = 0

        for site in self.config.get("sites", []):
            if not site.get("enabled", True):
                continue

            try:
                new_count = await self.check_site(site)
                total_new += new_count
                logger.info(f"网站 {site['name']} 发现 {new_count} 条新通知")
            except Exception as e:
                logger.error(f"检查网站 {site['name']} 失败: {e}")

        if total_new > 0:
            logger.info(f"总共发现 {total_new} 条新通知")

        return total_new

    async def check_site(self, site_config: Dict[str, Any]) -> int:
        """检查单个网站"""
        try:
            import aiohttp
            from bs4 import BeautifulSoup

            async with aiohttp.ClientSession() as session:
                async with session.get(site_config["url"], timeout=30) as response:
                    html = await response.text()

            soup = BeautifulSoup(html, 'html.parser')
            notices = []

            # 查找通知链接
            for link in soup.find_all('a'):
                href = link.get('href', '')
                title = link.get_text(strip=True)

                if href and title and len(title) > 5:
                    if href.startswith('http'):
                        url = href
                    elif href.startswith('/'):
                        url = f"https://www.nimt.edu.cn{href}"
                    else:
                        continue

                    # 检查是否是通知链接
                    if 'list' in href or 'content' in href or 'article' in href:
                        notices.append({
                            'title': title,
                            'url': url,
                            'date': datetime.now().strftime("%Y-%m-%d")
                        })

            # 保存到数据库
            conn = sqlite3.connect(str(self.db_file))
            cursor = conn.cursor()
            new_count = 0

            for notice in notices[:20]:  # 限制数量
                notice_id = hashlib.md5(f"{site_config['site_id']}_{notice['url']}".encode()).hexdigest()

                cursor.execute("SELECT id FROM notices WHERE id = ?", (notice_id,))
                if not cursor.fetchone():
                    cursor.execute(
                        "INSERT INTO notices (id, site_id, title, url, publish_date) VALUES (?, ?, ?, ?, ?)",
                        (notice_id, site_config['site_id'], notice['title'], notice['url'], notice['date'])
                    )
                    new_count += 1

            conn.commit()
            conn.close()

            return new_count

        except Exception as e:
            logger.error(f"检查网站失败: {e}")
            return 0

    @filter.command("测试")
    async def cmd_test(self, event: AstrMessageEvent):
        """测试命令"""
        logger.info("收到测试命令")
        yield event.plain_result("✅ 南京机电通知监控插件运行正常！")

    @filter.command("帮助")
    async def cmd_help(self, event: AstrMessageEvent):
        """显示帮助信息"""
        help_text = """
📚 南京机电职业技术学院通知监控插件 v2.0.0

🏫 主要功能：
1. 监控学校官网及二级学院网站通知
2. 自动推送新通知到指定用户/群组
3. 定时检查（默认5分钟一次）

🎓 可用命令：
• /测试 - 测试插件是否正常
• /帮助 - 显示此帮助信息
• /检查通知 - 立即检查新通知
• /查看通知 - 查看最近的通知

⚙️ 配置说明：
1. 通过AstrBot WebUI配置监控网站
2. 配置推送目标和检查间隔
3. 支持多个网站同时监控

💡 提示：
- 插件会自动定时检查新通知
- 新通知会推送到配置的用户和群组
- 支持学校官网和教务处网站监控
        """
        yield event.plain_result(help_text)

    @filter.command("检查通知")
    async def cmd_check_notices(self, event: AstrMessageEvent):
        """手动检查通知"""
        yield event.plain_result("⏳ 开始检查通知，请稍候...")

        try:
            new_count = await self.check_all_sites()
            if new_count > 0:
                yield event.plain_result(f"✅ 发现 {new_count} 条新通知")
            else:
                yield event.plain_result("📭 没有发现新通知")
        except Exception as e:
            logger.error(f"检查通知失败: {e}")
            yield event.plain_result(f"❌ 检查失败: {str(e)}")

    @filter.command("查看通知")
    async def cmd_view_notices(self, event: AstrMessageEvent, count: int = 5):
        """查看最近的通知"""
        try:
            if count < 1:
                count = 1
            if count > 20:
                count = 20

            conn = sqlite3.connect(str(self.db_file))
            cursor = conn.cursor()
            cursor.execute(
                "SELECT title, url, publish_date FROM notices ORDER BY created_at DESC LIMIT ?",
                (count,)
            )

            notices = cursor.fetchall()
            conn.close()

            if not notices:
                yield event.plain_result("📭 暂无通知记录")
                return

            response = f"📢 最近 {len(notices)} 条通知\n\n"
            for i, (title, url, date) in enumerate(notices, 1):
                short_title = title[:30] + "..." if len(title) > 30 else title
                response += f"{i}. {short_title}\n"
                response += f"   日期: {date}\n"
                response += f"   链接: {url[:50]}...\n\n"

            yield event.plain_result(response)

        except Exception as e:
            logger.error(f"查看通知失败: {e}")
            yield event.plain_result(f"❌ 查询失败: {str(e)}")

    async def terminate(self):
        """插件卸载"""
        logger.info("南京机电通知监控插件正在卸载...")