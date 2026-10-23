"""SQLAlchemy 声明式基类与通用 mixin。"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import DateTime, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# 用 CURRENT_TIMESTAMP 而不是 func.now()：
# func.now() 在 MySQL 方言里会渲染成 `now()`（带括号），
# 虽然 MySQL 8 接受，但不够标准，且迁移脚本里的字面量
# 与模型渲染结果不一致时很难排查。CURRENT_TIMESTAMP 是
# SQL 标准写法，MySQL 与 SQLite 渲染结果一致。
_NOW = text("CURRENT_TIMESTAMP")


def _utcnow() -> datetime:
    """当前 UTC 时间（无时区），精确到秒。

    naive datetime 是刻意的：MySQL 的 DATETIME 不带时区，
    统一存 UTC，读出来再按需转本地时间，避免混用。

    **必须截断到秒**：MySQL 的 DATETIME 默认精度是秒，
    不存微秒。如果 Python 侧带上微秒，flush 后对象里的值
    （带微秒）和回读到的值（被截断）就不一致——
    表现为「updated_at 没变」这类难查的问题。
    """
    return datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)


class Base(DeclarativeBase):
    pass


class TimestampMixin:
    # default 和 server_default 都要有：
    #
    # server_default 让数据库在直接 INSERT（绕过 ORM）时也能填上，
    # 保证数据完整。
    #
    # default 让 Python 侧在 flush 之前就有值。这点很关键：
    # 只有 server_default 时，flush 之后要拿到 created_at 必须
    # 回数据库查一次——SQLite 能用 RETURNING 直接拿到，
    # MySQL 不支持 RETURNING，只能 SELECT 回读，
    # 而那个隐式 SELECT 在 async 上下文里会抛
    #   MissingGreenlet: greenlet_spawn has not been called
    # 表现为注册接口直接 500。
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=_utcnow,
        server_default=_NOW,
        nullable=False,
        index=True,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=_utcnow,
        server_default=_NOW,
        # onupdate 是应用层行为：SQLAlchemy 在 UPDATE 时会带上
        # updated_at = CURRENT_TIMESTAMP。MySQL 不会自动更新
        # DATETIME 列，因此这一条不能省——省了就永远停留在创建时间。
        onupdate=_utcnow,
        nullable=False,
    )


class SoftDeleteMixin:
    """软删除，支持站点博主要求下架时一键清除该站全部数据。"""

    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime, default=None, index=True
    )