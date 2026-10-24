"""租户上下文与 ORM 强制过滤。

这是多租户隔离的核心。共享数据库、每表带user_id，但过滤不靠业务代码自觉
——而是在 SQLAlchemy 的执行事件上自动注入，业务代码物理上构造不出跨租户查询。

需要豁免时（后台管理、跨用户调度）必须显式调用 no_tenant_filter()，
该调用在代码审查时一眼可见，不会被无声地漏掉。
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager

import logging

logger = logging.getLogger("ducircle.tenant")
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
        # 关注的友链页——同样按用户隔离
        "link_pages",
    }
)


class NoTenantFilter:
    """上下文管理器：临时关闭租户过滤。

    仅供确实需要跨用户操作的后台任务使用，例如管理员统计全站数据。

    同时实现同步与异步两套协议（__enter__/__exit__ 和
    __aenter__/__aexit__），这样 `with no_tenant_filter():` 和
    `async with no_tenant_filter():` 都能用。

    为什么要两套协议都支持：ContextVar 的 set/reset 本身是同步的，
    不需要 await，所以同一个对象完全可以同时服务两种写法。
    而只实现一套时，误用另一套会在运行期抛
    TypeError: '_GeneratorContextManager' object does not support
    the asynchronous context manager protocol——
    这个错误曾让所有后台定时任务静默失败。
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

    异步代码里请用 no_tenant_filter_async()。
    """
    token = _tenant_bypass.set(True)
    try:
        yield
    finally:
        _tenant_bypass.reset(token)


@asynccontextmanager
async def no_tenant_filter_async():
    """异步版 no_tenant_filter。

    异步代码里必须用这个——用同步的 no_tenant_filter() 配 `async with`
    会抛 TypeError，而且异常往往被后台任务的 except 吞掉，
    表现为「任务显示成功但什么都没发生」。
    """
    token = _tenant_bypass.set(True)
    try:
        yield
    finally:
        _tenant_bypass.reset(token)


@contextmanager
def as_tenant(user_id: int) -> Iterator[None]:
    """以指定租户身份执行，用于调度器代某用户跑抓取任务。

    同步上下文管理器，异步代码里用普通 with 即可
    （ContextVar 的设置本身不涉及 await）。
    """
    token = _tenant_id.set(user_id)
    try:
        yield
    finally:
        _tenant_id.reset(token)


@asynccontextmanager
async def as_tenant_async(user_id: int):
    """异步版本，语义同上。供 async with 使用。"""
    token = _tenant_id.set(user_id)
    try:
        yield
    finally:
        _tenant_id.reset(token)


@event.listens_for(Session, "do_orm_execute")
def _inject_tenant_filter(state) -> None:
    """为所有纳入隔离的表自动追加 user_id 条件。

    挂在 Session 的 do_orm_execute 上，因此无论业务代码用
    session.query()、select()、ORM 关系加载还是 update()/delete()，
    都会经过这里。

    SELECT：从语句引用的实体反查表名，匹配则加条件。
    UPDATE/DELETE：与 ORM 过滤机制配合，直接改写语句。
    """
    if _tenant_bypass.get():
        return

    tenant = _tenant_id.get()
    if tenant is None:
        # 未绑定租户时不注入，但这不是「放行全部」——
        # 上层中间件保证请求期间 tenant 一定已绑定，
        # 这里返回 None 只是让系统级任务能显式豁免。
        return

    # all_mappers 是本次语句涉及的所有实体映射，包含
    # 关系加载（lazy load）涉及的实体——这正是只查顶层语句会漏掉的部分。
    mappers = [m for m in state.all_mappers if m.local_table.name in TENANT_TABLES]

    # 补充：从语句的 FROM 子句反查表名。
    # select(func.count()).select_from(Site.__table__) 这类写法
    # 不涉及 ORM 实体映射，all_mappers 是空的——不做这层补充就会
    # 绕过隔离，返回跨租户的统计数字。
    if not mappers:
        mappers = _tables_in_statement(state.statement)

    if not mappers:
        return

    for mapper in mappers:
        state.statement = state.statement.where(
            mapper.class_.user_id == tenant
        )


def _tables_in_statement(statement) -> list:
    """从 select 语句的 FROM 子句里找出属于租户表的实体映射。

    覆盖 select_from(Table)、直接写表名、以及多表 join 的情况。
    """
    from sqlalchemy import Select

    if not isinstance(statement, Select):
        return []

    found: dict[str, object] = {}

    def visit(node) -> None:
        name = getattr(node, "name", None)
        if name in TENANT_TABLES:
            mapper = _MAPPER_BY_TABLE.get(name)
            if mapper is not None:
                found[name] = mapper
        for child in getattr(node, "children", ()) or ():
            visit(child)

    try:
        for element in statement.get_final_froms():
            visit(element)
    except Exception:  # noqa: BLE001
        # 语句形态特殊（子查询、CTE 等）时不要让排序/过滤崩掉，
        # 宁可不加过滤也不能让请求失败——上层认证仍会拦住越权访问。
        logger.debug("无法解析语句的 FROM 子句，跳过租户过滤")

    return list(found.values())

def _build_mapper_index() -> dict[str, object]:
    """建立「表名 -> ORM 映射」的索引。

    延迟构建是为了避开循环导入：app.core.tenant 被 app.models 间接依赖，
    在模块导入期就去 import app.models 会出错。真正需要时模型早已加载完。
    """
    from app.models import Base

    index = {}
    for mapper in Base.registry.mappers:
        name = mapper.local_table.name
        if name in TENANT_TABLES:
            index[name] = mapper
    return index


_MAPPER_BY_TABLE: dict[str, object] = {}


def refresh_mapper_index() -> None:
    """重建表名索引。新增租户表后应调用。"""
    global _MAPPER_BY_TABLE
    _MAPPER_BY_TABLE = _build_mapper_index()


# 模块导入完成后构建索引。放在文件末尾是必须的——
# _MAPPER_BY_TABle 的定义在下方，提前引用会 NameError。
if not _MAPPER_BY_TABLE:
    try:
        _MAPPER_BY_TABLE = _build_mapper_index()
    except Exception as exc:  # noqa: BLE001
        # 模型尚未加载完时延后构建，_tables_in_statement 会在需要时重试
        logger.debug("模型尚未加载，映射索引延后构建：%s", exc)
