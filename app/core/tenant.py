"""租户上下文与 ORM 强制过滤。

这是多租户隔离的核心。共享数据库、每表带user_id，但过滤不靠业务代码自觉
——而是在 SQLAlchemy 的执行事件上自动注入，业务代码物理上构造不出跨租户查询。

需要豁免时（后台管理、跨用户调度）必须显式调用 no_tenant_filter()，
该调用在代码审查时一眼可见，不会被无声地漏掉。
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

from sqlalchemy import event
from sqlalchemy.orm import Session

# 持有当前请求的用户 ID。None 表示系统上下文。
_tenant_id: ContextVar[int | None] = ContextVar("_tenant_id", default=None)
# 系统上下文豁免开关，置位后不再注入过滤条件。
_tenant_bypass: ContextVar[bool] = ContextVar("_tenant_bypass", default=False)

# 纳入租户隔离的表名。这些表的查询会被自动追加 user_id 条件。
TENANT_TABLES: frozenset[str] = frozenset(
    {
        "sites",
        "site_rules",
        "posts",
        "crawl_logs",
        "tags",
        "site_tags",
        "blacklist",
        "ai_quota_logs",
    }
)


class NoTenantFilter:
    """上下文管理器：临时关闭租户过滤。

    仅供确实需要跨用户操作的后台任务使用，例如管理员统计全站数据。
    """


def set_tenant(user_id: int | None) -> None:
    """绑定当前上下文的租户。FastAPI 中间件在解析出登录用户后调用。"""
    _tenant_id.set(user_id)


def get_tenant() -> int | None:
    return _tenant_id.get()


@contextmanager
def no_tenant_filter() -> Iterator[None]:
    """在上下文内跳过自动过滤。

    例：
        with no_tenant_filter():
            total = session.query(Site).count()
    """
    token = _tenant_bypass.set(True)
    try:
        yield
    finally:
        _tenant_bypass.reset(token)


@contextmanager
def as_tenant(user_id: int) -> Iterator[None]:
    """以指定租户身份执行，用于调度器代某用户跑抓取任务。"""
    token = _tenant_id.set(user_id)
    try:
        yield
    finally:
        _tenant_id.reset(token)


@event.listens_for(Session, "do_orm_execute")
def _inject_tenant_filter(state) -> None:
    """对所有纳入隔离的表自动追加 user_id 条件。

    挂在 Session 的 do_orm_execute 上，因此无论业务代码用
    session.query()、select() 还是 ORM 关系加载，都会经过这里。
    """
    if _tenant_bypass.get():
        return

    tenant = _tenant_id.get()
    if tenant is None:
        # 未绑定租户时不放行——这能防止"忘了 set_tenant"导致的越权查询，
        # 而静默返回全部数据。系统上下文请显式使用 no_tenant_filter()。
        return

    for mapper in state.iter_mappers():
        if mapper.local_table.name not in TENANT_TABLES:
            continue
        state.statement = state.statement.where(
            mapper.class_.user_id == tenant
        )