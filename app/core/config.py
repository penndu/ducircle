"""应用配置。所有可调参数集中在此，通过环境变量或 .env 覆盖。"""

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # ---- 基础 ----
    app_name: str = "友链朋友圈"
    debug: bool = False
    secret_key: str = "change-me-in-production"
    base_url: str = "http://localhost:8000"

    # ---- 数据库 ----
    mysql_host: str = "mysql"
    mysql_port: int = 3306
    mysql_user: str = "ducircle"
    mysql_password: str = ""
    mysql_database: str = "ducircle"

    # ---- Redis ----
    redis_url: str = "redis://redis:6379/0"

    # ---- 抓取 ----
    # 单站超时（秒）
    crawl_timeout: int = 30
    # 单站失败重试次数，指数退避
    crawl_retries: int = 3
    # 每用户同时抓取的站点数上限
    max_concurrent_per_user: int = 10
    # 全局同时抓取的站点数上限
    max_concurrent_global: int = 50
    # 是否启用自定义规则（用户可关闭以纯自动方式运行）
    enable_custom_rules: bool = True
    # User-Agent，公开服务必须可识别
    user_agent: str = (
        "ducircleBot/2.0 (+https://ducircle.dusays.com; "
        "friendly circle-of-friends aggregator)"
    )
    # 同一站点两次抓取之间的最小间隔（秒），避免重复打扰对方
    crawl_min_interval: int = 1800
    # 是否遵守 robots.txt
    respect_robots: bool = True
    # 每个友链保留的最大文章数
    max_posts_per_site: int = 10
    # 过期文章清理天数，0 表示不清理
    outdate_clean_days: int = 90

    # ---- 限流 ----
    # 只读 API 每用户 QPS
    rate_limit_qps: int = 5
    rate_limit_burst: int = 15

    # ---- AI 摘要 ----
    ai_enabled: bool = True
    ai_platform_provider: str = "siliconflow"
    ai_platform_api_key: str = ""
    ai_platform_model: str = "Qwen/Qwen3-8B"
    # 每用户每日免费摘要篇数
    ai_daily_quota: int = 10
    # 送入模型的正文最大字符数
    ai_max_chars: int = 8000
    ai_timeout: int = 60

    # ---- 调度 ----
    # 定时抓取任务（cron 格式），默认每天 4 次
    crawl_cron: str = "0 0,6,12,18 * * *"
    # 过期清理任务，每天 3 点
    cleanup_cron: str = "0 3 * * *"
    # 统计快照任务，每天 4 点
    stats_cron: str = "0 4 * * *"
    # AI 摘要任务，每小时一次。
    # 放小时级而不是每天——文章流是滚动的，新文章要尽快有摘要，
    # 否则用户点开看到的还是「待摘要」。
    ai_cron: str = "0 * * * *"
    # 友链页检测任务，每天 5 点。
    # 友链变动很慢（一个月加一两个朋友是常态），
    # 每天一次足够，也省得频繁去打扰对方的站。
    link_page_cron: str = "0 5 * * *"
    scheduler_enabled: bool = True

    # ---- 缓存 ----
    cache_ttl: int = 60
    # 缓存有效内容压缩阈值，正文小于该值直接缓存原HTML
    cache_max_html_bytes: int = 2 * 1024 * 1024

    @property
    def database_url(self) -> str:
        return (
            f"mysql+asyncmy://{self.mysql_user}:{self.mysql_password}"
            f"@{self.mysql_host}:{self.mysql_port}/{self.mysql_database}"
            "?charset=utf8mb4"
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()