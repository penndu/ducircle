"""用户模型。"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin


class User(Base, TimestampMixin):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(32), unique=True, nullable=False,
                                          index=True)
    password_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    email: Mapped[str | None] = mapped_column(String(128), default=None)

    # 只读接口用的 Key。数据库只存哈希，明文仅在注册成功时返回一次。
    api_key_hash: Mapped[str | None] = mapped_column(String(64), default=None,
                                                      unique=True)

    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # AI 摘要配置：用户可自带 Key 以摆脱平台额度限制
    ai_provider: Mapped[str | None] = mapped_column(String(32), default=None)
    ai_api_key: Mapped[str | None] = mapped_column(String(256), default=None)
    ai_model: Mapped[str | None] = mapped_column(String(128), default=None)
    ai_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    ai_daily_quota_override: Mapped[int | None] = mapped_column(
        Integer, default=None, comment="为空则用平台默认值"
    )

    # 每用户抓取并发上限，NULL 表示用平台默认值
    max_concurrent_override: Mapped[int | None] = mapped_column(Integer, default=None)

    # ---- 采集频率 ----
    # cron 表达式（5 段）。空表示用平台默认。
    #
    # 为什么存 cron 而不是「每隔 N 小时」：
    # 「每天 3 点」这种需求 cron 能表达，「每隔 90 分钟」不能。
    # 用户是博客主，让他说人话比让他做算术强。
    #
    # 文章采集：控制这个用户的站点多久抓一次
    crawl_cron: Mapped[str | None] = mapped_column(String(64), default=None)
    # 友链采集：控制这个用户的友链页多久检测一次
    link_page_cron: Mapped[str | None] = mapped_column(String(64), default=None)

    # 上次执行采集任务的时间。用来判断「到点了没」——
    # 规则是「最近一个 cron 触发点是否晚于这个时间」。
    last_crawl_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)
    last_link_page_scan_at: Mapped[datetime | None] = mapped_column(
        DateTime, default=None
    )

    last_login_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)

    sites: Mapped[list["Site"]] = relationship(  # noqa: F821
        back_populates="user", cascade="all, delete-orphan"
    )

    @property
    def has_own_ai_key(self) -> bool:
        return bool(self.ai_api_key)

    def quota_limit(self, default: int) -> int:
        return self.ai_daily_quota_override or default