"""应用配置。所有可调参数集中在此，通过环境变量或 .env 覆盖。"""

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


def _read_version() -> str:
    """读语义版本号（如 1.0.0）。

    **单一真源**：版本号只写在 pyproject.toml 一处，
    API 文档、/health 都用它——散落各处迟早会漏改。

    生产镜像里没有 pyproject.toml 时，退回 APP_VERSION 环境变量
    （CI 用 --build-arg 注入）。

    ⚠️ 别用 DUCIRCLE_VERSION——那是**构建版本**（时间戳 202611121026），
    不是语义版本，两个混在一起会让人分不清跑的是哪个版本。
    """
    import os

    pyproject = Path(__file__).resolve().parents[2] / "pyproject.toml"
    try:
        for line in pyproject.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped.startswith("version"):
                _, _, value = stripped.partition("=")
                version = value.strip().strip('"').strip("'")
                if version:
                    return version
    except OSError:
        pass

    return os.environ.get("APP_VERSION", "").strip() or "0.0.0"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # ---- 基础 ----
    app_name: str = "友链朋友圈"
    app_version: str = Field(default_factory=_read_version)
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

    # ---- 跨域 ----
    # 允许哪些站点从浏览器直接调只读 API（/v1/*）。
    #
    # 默认 "*" —— 不限制调用网站。博主把博客放在哪个域名都不用改配置，
    # 这是「API 支持跨站使用」的默认行为。
    # 安全性由 API Key 保证：/v1/* 只读、不用 Cookie，
    # 放开来源不会带来 CSRF 风险（后台 /api/* 不开放跨域）。
    #
    # 想收紧就填逗号分隔的域名白名单，例如：
    #   CORS_ORIGINS=https://dusays.com,https://www.dusays.com
    cors_origins: str = "*"

    # ---- 注册 ----
    # 是否开放自助注册。
    #
    # 关闭后 /web/register 会提示「暂不开放注册」，
    # 新账号只能由管理员在「用户管理」页创建。
    # 公共实例默认开放——平台的价值在于让访客能自己用起来。
    allow_registration: bool = True

    # ---- AI 摘要 ----
    ai_enabled: bool = True
    ai_platform_provider: str = "siliconflow"
    ai_platform_api_key: str = ""
    ai_platform_model: str = "Qwen/Qwen3-8B"
    # 每用户每日免费摘要篇数。0 = 平台不提供免费额度，
    # 用户必须自己填 API Key 才能生成摘要。
    # 保留这个开关是为了管理员仍可以给个别用户单独开额度。
    ai_daily_quota: int = 0
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