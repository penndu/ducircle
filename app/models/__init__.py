"""模型汇总导出。导入本模块即完成所有表注册，alembic 才能 autogenerate。"""

from app.models.ai_config import AIProviderConfig
from app.models.base import Base
from app.models.post import AIQuotaLog, CrawlLog, Post
from app.models.redeem import RedeemBatch, RedeemCode
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
    "AIProviderConfig",
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
    "RedeemBatch",
    "RedeemCode",
]