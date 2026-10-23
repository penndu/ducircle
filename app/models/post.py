"""文章、摘要缓存、抓取日志模型。"""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, SoftDeleteMixin, TimestampMixin


class Post(Base, TimestampMixin, SoftDeleteMixin):
    __tablename__ = "posts"
    __table_args__ = (
        # 去重键：同一用户下同 guid 只留一条
        UniqueConstraint("user_id", "guid_hash", name="uq_post_user_guid"),
        # 文章流分页走这条索引
        Index("ix_posts_user_created", "user_id", "created"),
        Index("ix_posts_user_score", "user_id", "ai_score"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    site_id: Mapped[int] = mapped_column(
        ForeignKey("sites.id", ondelete="CASCADE"), nullable=False, index=True
    )

    title: Mapped[str] = mapped_column(String(512), nullable=False)
    link: Mapped[str] = mapped_column(String(1024), nullable=False)
    # 规范化 URL 的 SHA1。博主换域名后老链接仍能命中同一篇文章。
    guid_hash: Mapped[str] = mapped_column(String(40), nullable=False)

    created: Mapped[datetime | None] = mapped_column(DateTime, default=None)
    updated: Mapped[datetime | None] = mapped_column(DateTime, default=None)
    # published 与 updated 都缺失时的兜底，避免文章显示成"今天"
    crawled_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.now, nullable=False
    )

    # 实际生效的解析策略，便于统计各层贡献率
    strategy: Mapped[str | None] = mapped_column(String(32), default=None)

    # AI 摘要字段
    summary: Mapped[str | None] = mapped_column(Text, default=None)
    ai_model: Mapped[str | None] = mapped_column(String(128), default=None)
    ai_score: Mapped[float | None] = mapped_column(
        Float, default=None, comment="推荐分，0~10，用于价值排序"
    )
    ai_reason: Mapped[str | None] = mapped_column(Text, default=None)
    summary_content_hash: Mapped[str | None] = mapped_column(
        String(40), default=None, comment="正文哈希，正文未变则不重复生成"
    )
    summary_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)
    # AI 摘要被人工修正后置位，不再自动覆盖
    summary_edited: Mapped[bool] = mapped_column(Boolean, default=False,
                                                  nullable=False)

    site: Mapped["Site"] = relationship(back_populates="posts")  # noqa: F821


class CrawlLog(Base, TimestampMixin):
    """每次抓取的记录。抓取失败列表和成功率统计都基于这张表。"""

    __tablename__ = "crawl_logs"
    __table_args__ = (
        Index("ix_logs_user_created", "user_id", "created_at"),
        Index("ix_logs_user_status", "user_id", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    site_id: Mapped[int | None] = mapped_column(
        ForeignKey("sites.id", ondelete="CASCADE"), default=None, index=True
    )
    site_url: Mapped[str] = mapped_column(String(1024), nullable=False)

    # success / failed / skipped
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    strategy: Mapped[str | None] = mapped_column(String(32), default=None)
    error: Mapped[str | None] = mapped_column(Text, default=None)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # 本次抓到几篇文章
    post_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # HTTP 状态码，便于区分 404/403/超时
    http_status: Mapped[int | None] = mapped_column(Integer, default=None)


class AIQuotaLog(Base):
    """AI 额度消耗。按 (用户, 日期) 聚合判断是否超额。"""

    __tablename__ = "ai_quota_logs"
    __table_args__ = (
        UniqueConstraint("user_id", "quota_date", "post_id", name="uq_quota_user_date_post"),
        Index("ix_quota_user_date", "user_id", "quota_date"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    post_id: Mapped[int] = mapped_column(
        ForeignKey("posts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    quota_date: Mapped[date] = mapped_column(Date, nullable=False)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    model: Mapped[str | None] = mapped_column(String(128), default=None)
    # 用户自带 Key 的调用不计入平台额度
    from_user_key: Mapped[bool] = mapped_column(Boolean, default=False,
                                                 nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.now, nullable=False
    )