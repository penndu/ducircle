"""模型汇总导出。导入本模块即完成所有表注册，alembic 才能 autogenerate。"""

from app.models.base import Base
from app.models.post import AIQuotaLog, CrawlLog, Post
from app.models.site import (
    Blacklist,
    LinkPage,
    RuleField,
    Site,
    SiteRule,
    SiteSource,
    SiteTag,
    Tag,
)
from app.models.user import User

__all__ = [
    "Base",
    "User",
    "Site",
    "SiteRule",
    "SiteSource",
    "RuleField",
    "Tag",
    "SiteTag",
    "Blacklist",
    "LinkPage",
    "Post",
    "CrawlLog",
    "AIQuotaLog",
]