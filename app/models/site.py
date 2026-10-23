"""友链站点、自定义规则、标签、黑名单模型。"""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, SoftDeleteMixin, TimestampMixin


class SiteSource(str, Enum):
    """站点来源。"""

    LINK_PAGE = "link_page"  # 从友链页解析得到
    MANUAL = "manual"  # 用户手动添加
    OPML = "opml"  # OPML 导入


class Site(Base, TimestampMixin, SoftDeleteMixin):
    __tablename__ = "sites"
    __table_args__ = (
        Index("ix_sites_user_active", "user_id", "active"),
        Index("ix_sites_user_hash", "user_id", "url_hash", unique=True),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )

    name: Mapped[str] = mapped_column(String(256), nullable=False)
    url: Mapped[str] = mapped_column(String(1024), nullable=False)
    url_hash: Mapped[str] = mapped_column(String(40), nullable=False,
                                           comment="规范化后主机名的 SHA1，用于同站去重")
    feed_url: Mapped[str | None] = mapped_column(String(1024), default=None)
    avatar: Mapped[str | None] = mapped_column(String(1024), default=None)
    description: Mapped[str | None] = mapped_column(Text, default=None)
    note: Mapped[str | None] = mapped_column(Text, default=None, comment="用户备注")

    source: Mapped[str] = mapped_column(
        String(16), default=SiteSource.MANUAL.value, nullable=False
    )
    # 记录是从哪个友链页发现的，便于展示"我的好友来自谁的友链页"
    discovered_from: Mapped[str | None] = mapped_column(String(1024), default=None)

    # 生效的解析策略：feed / fingerprint:hugo / heuristic / custom / failed
    strategy: Mapped[str | None] = mapped_column(String(32), default=None)
    framework: Mapped[str | None] = mapped_column(String(32), default=None)

    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    error: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    error_message: Mapped[str | None] = mapped_column(Text, default=None)
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0,
                                                      nullable=False)

    last_crawled_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)
    last_success_at: Mapped[datetime | None] = mapped_column(
        DateTime, default=None, index=True
    )
    # 连续失败后自动降频调度，避免反复骚扰已下线的站点
    crawl_interval_hours: Mapped[int] = mapped_column(Integer, default=12,
                                                       nullable=False)

    user: Mapped["User"] = relationship(back_populates="sites")  # noqa: F821
    rules: Mapped[list["SiteRule"]] = relationship(
        back_populates="site", cascade="all, delete-orphan"
    )
    posts: Mapped[list["Post"]] = relationship(  # noqa: F821
        back_populates="site", cascade="all, delete-orphan"
    )

    @property
    def is_zombie(self) -> bool:
        """僵尸判定：90 天内没有任何成功抓取。"""
        if self.last_success_at is None:
            return True
        from datetime import timedelta

        from app.core.config import get_settings

        days = get_settings().outdate_clean_days or 90
        return (
            datetime.now() - self.last_success_at > timedelta(days=days)
        )


class RuleField(str, Enum):
    """可自定义规则的字段。"""

    TITLE = "title"
    LINK = "link"
    CREATED = "created"
    UPDATED = "updated"
    AUTHOR = "author"
    AVATAR = "avatar"


class SiteRule(Base, TimestampMixin):
    """用户自定义抓取规则，优先级高于所有自动策略。"""

    __tablename__ = "site_rules"
    __table_args__ = (
        Index("ix_rules_site_priority", "site_id", "priority"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    site_id: Mapped[int] = mapped_column(
        ForeignKey("sites.id", ondelete="CASCADE"), nullable=False, index=True
    )

    field: Mapped[str] = mapped_column(String(16), nullable=False)
    selector: Mapped[str] = mapped_column(String(512), nullable=False)
    # text 表示取文本内容，其余表示取同名 HTML 属性
    attr: Mapped[str] = mapped_column(String(32), default="text", nullable=False)
    # 数值越大越优先。同一字段内按顺序尝试，命中即短路。
    priority: Mapped[int] = mapped_column(Integer, default=100, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    site: Mapped["Site"] = relationship(back_populates="rules")


class Tag(Base, TimestampMixin):
    __tablename__ = "tags"
    __table_args__ = (UniqueConstraint("user_id", "name", name="uq_tag_user_name"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    color: Mapped[str | None] = mapped_column(String(16), default=None)


class SiteTag(Base):
    __tablename__ = "site_tags"
    __table_args__ = (UniqueConstraint("site_id", "tag_id", name="uq_site_tag"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    site_id: Mapped[int] = mapped_column(
        ForeignKey("sites.id", ondelete="CASCADE"), nullable=False, index=True
    )
    tag_id: Mapped[int] = mapped_column(
        ForeignKey("tags.id", ondelete="CASCADE"), nullable=False, index=True
    )


class Blacklist(Base, TimestampMixin):
    """黑名单，支持正则。用于过滤不想抓取的站点。"""

    __tablename__ = "blacklist"
    __table_args__ = (
        Index("ix_blacklist_user_pattern", "user_id", "pattern", unique=True),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    pattern: Mapped[str] = mapped_column(String(512), nullable=False)
    is_regex: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    note: Mapped[str | None] = mapped_column(String(256), default=None)


class LinkPage(Base, TimestampMixin):
    """关注的友链页。

    填一个友链页地址（如 https://example.com/friends/），
    系统会解析出页面上的所有友情链接并加入友链列表，
    之后定期重新解析，发现新友链就自动补进来。

    与 Site 的区别：Site 是「一个要抓取文章的博客」，
    LinkPage 是「一张写着别人博客地址的清单页」。
    """

    __tablename__ = "link_pages"
    __table_args__ = (
        Index("ix_link_pages_user_hash", "user_id", "url_hash", unique=True),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )

    url: Mapped[str] = mapped_column(String(1024), nullable=False)
    url_hash: Mapped[str] = mapped_column(
        String(40), nullable=False, comment="规范化后 URL 的 SHA1，用于同页去重"
    )
    name: Mapped[str | None] = mapped_column(
        String(256), default=None, comment="页面标题，解析时自动填"
    )
    note: Mapped[str | None] = mapped_column(String(256), default=None)

    # 上次解析发现的友链数
    found_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # 上次解析新增了几条
    added_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)
    last_error: Mapped[str | None] = mapped_column(Text, default=None)

    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)