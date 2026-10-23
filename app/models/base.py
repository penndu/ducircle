"""SQLAlchemy 声明式基类与通用 mixin。"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# 用 CURRENT_TIMESTAMP 而不是 func.now()：
# func.now() 在 MySQL 方言里会渲染成 `now()`（带括号），
# 虽然 MySQL 8 接受，但不够标准，且迁移脚本里的字面量
# 与模型渲染结果不一致时很难排查。CURRENT_TIMESTAMP 是
# SQL 标准写法，MySQL 与 SQLite 渲染结果一致。
_NOW = text("CURRENT_TIMESTAMP")


class Base(DeclarativeBase):
    pass


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=_NOW, nullable=False, index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        server_default=_NOW,
        # onupdate 是应用层行为：SQLAlchemy 在 UPDATE 时会带上
        # updated_at = CURRENT_TIMESTAMP。MySQL 不会自动更新
        # DATETIME 列，因此这一条不能省——省了就永远停留在创建时间。
        onupdate=_NOW,
        nullable=False,
    )


class SoftDeleteMixin:
    """软删除，支持站点博主要求下架时一键清除该站全部数据。"""

    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime, default=None, index=True
    )