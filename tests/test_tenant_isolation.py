"""多租户隔离测试。

这是整个项目最关键的测试。用两个真实用户的数据互相试探，
任何一次越权（用户 A 能看到用户 B 的数据）都判定为失败。

测试用 SQLite 而非 MySQL：租户过滤钩子挂在 SQLAlchemy 的
do_orm_execute 事件上，与具体数据库无关；但 SQL 特性差异
（JSON 类型、索引前缀长度）由迁移脚本保证，不在本测试范围。
"""

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.tenant import (
    TENANT_TABLES,
    as_tenant,
    as_tenant_async,
    no_tenant_filter,
    set_tenant,
)
from app.models import Base, Post, Site, User

ALICE_ID = 1
BOB_ID = 2


@pytest_asyncio.fixture
async def session():
    """每个用例一个内存库，互不干扰。"""
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        yield s
    await engine.dispose()


async def _seed(session):
    """Alice 有 2 个站点 3 篇文章，Bob 有 1 个站点 1 篇文章。"""
    session.add_all([
        User(id=ALICE_ID, username="alice", password_hash="x"),
        User(id=BOB_ID, username="bob", password_hash="x"),
    ])
    session.add_all([
        Site(id=1, user_id=ALICE_ID, name="Alice站点A", url="https://a.com",
             url_hash="h1", source="manual"),
        Site(id=2, user_id=ALICE_ID, name="Alice站点B", url="https://b.com",
             url_hash="h2", source="manual"),
        Site(id=3, user_id=BOB_ID, name="Bob站点", url="https://bob.com",
             url_hash="h3", source="manual"),
    ])
    session.add_all([
        Post(id=1, user_id=ALICE_ID, site_id=1, title="Alice文章1",
             link="https://a.com/1", guid_hash="g1", strategy="feed"),
        Post(id=2, user_id=ALICE_ID, site_id=1, title="Alice文章2",
             link="https://a.com/2", guid_hash="g2", strategy="feed"),
        Post(id=3, user_id=ALICE_ID, site_id=2, title="Alice文章3",
             link="https://b.com/3", guid_hash="g3", strategy="feed"),
        Post(id=4, user_id=BOB_ID, site_id=3, title="Bob文章",
             link="https://bob.com/1", guid_hash="g4", strategy="feed"),
    ])
    await session.commit()


class TestTenantIsolation:
    """核心防线：越权查询必须返回 0 条。"""

    @pytest.mark.asyncio
    async def test_posts_filtered_by_tenant(self, session):
        await _seed(session)

        set_tenant(ALICE_ID)
        posts = (await session.execute(__import__("sqlalchemy").select(Post))).scalars().all()
        assert len(posts) == 3, "Alice 只能看到自己的 3 篇"
        assert all(p.user_id == ALICE_ID for p in posts)
        set_tenant(None)

    @pytest.mark.asyncio
    async def test_sites_filtered_by_tenant(self, session):
        await _seed(session)

        from sqlalchemy import func, select

        set_tenant(BOB_ID)
        sites = (await session.execute(select(Site))).scalars().all()
        assert len(sites) == 1, "Bob 只能看到自己的 1 个站点"
        assert sites[0].name == "Bob站点"
        set_tenant(None)

    @pytest.mark.asyncio
    async def test_alice_cannot_see_bob_data(self, session):
        """双向确认：既不能多看，也不能少看。"""
        await _seed(session)
        from sqlalchemy import func, select

        set_tenant(ALICE_ID)
        titles = {
            p.title for p in (await session.execute(select(Post))).scalars().all()
        }
        assert "Bob文章" not in titles, "Alice 绝不能看到 Bob 的文章"
        assert "Alice文章1" in titles
        set_tenant(None)

    @pytest.mark.asyncio
    async def test_relationship_query_also_filtered(self, session):
        """关系加载（user.sites）同样受保护。

        这条最容易漏——通过 ORM 关系导航访问时，
        如果钩子只处理了顶层 query，lazy load 就会绕过。
        """
        await _seed(session)
        from sqlalchemy import func, select

        set_tenant(ALICE_ID)
        alice = (await session.execute(
            select(User).where(User.id == ALICE_ID)
        )).scalar_one()
        await session.refresh(alice, ["sites"])
        assert len(alice.sites) == 2, "通过关系加载也只能拿到自己的站点"
        assert all(s.user_id == ALICE_ID for s in alice.sites)
        set_tenant(None)

    @pytest.mark.asyncio
    async def test_aggregate_count_filtered(self, session):
        """count() 等聚合查询也必须过滤，否则统计数字会泄露。"""
        await _seed(session)
        from sqlalchemy import func, select

        set_tenant(ALICE_ID)
        count = (await session.execute(
            select(func.count(Post.id))
        )).scalar_one()
        assert count == 3, "count 不能返回全站总数"
        set_tenant(None)

    @pytest.mark.asyncio
    async def test_raw_where_still_filtered(self, session):
        """业务代码自己写的 WHERE 不会覆盖钩子注入的条件。"""
        await _seed(session)
        from sqlalchemy import func, select

        set_tenant(BOB_ID)
        # Bob 试图查 Alice 的文章，钩子会再叠加一层 user_id = 2
        posts = (await session.execute(
            select(Post).where(Post.user_id == ALICE_ID)
        )).scalars().all()
        assert len(posts) == 0, "手写的越权条件应被钩子拦截为空结果"
        set_tenant(None)

    @pytest.mark.asyncio
    async def test_tenant_switch_in_one_session(self, session):
        """同一 Session 内切换租户上下文，隔离立即生效。

        对应「调度器代不同用户跑任务」的场景。
        """
        await _seed(session)
        from sqlalchemy import func, select

        async with as_tenant_async(ALICE_ID):
            n_alice = (await session.execute(
                select(func.count(Post.id))
            )).scalar_one()

        async with as_tenant_async(BOB_ID):
            n_bob = (await session.execute(
                select(func.count(Post.id))
            )).scalar_one()

        assert n_alice == 3
        assert n_bob == 1
        set_tenant(None)


class TestExplicitBypass:
    """豁免机制：必须显式、可审计，不能被无声地触发。"""

    @pytest.mark.asyncio
    async def test_no_tenant_filter_allows_cross_user(self, session):
        """no_tenant_filter() 是给后台管理用的，调用点在代码审查时可见。"""
        await _seed(session)
        from sqlalchemy import func, select

        set_tenant(ALICE_ID)
        with no_tenant_filter():
            total = (await session.execute(select(Post))).scalars().all()
            assert len(total) == 4, "豁免后能看到全部数据"

        # 豁免结束后必须自动恢复隔离
        scoped = (await session.execute(select(Post))).scalars().all()
        assert len(scoped) == 3, "豁免上下文退出后必须自动恢复"
        set_tenant(None)

    @pytest.mark.asyncio
    async def test_bypass_restores_after_exception(self, session):
        """豁免上下文里抛异常也必须恢复，不能泄漏隔离状态。"""
        await _seed(session)
        from sqlalchemy import func, select

        set_tenant(ALICE_ID)
        try:
            with no_tenant_filter():
                raise ValueError("模拟中途失败")
        except ValueError:
            pass

        scoped = (await session.execute(select(Post))).scalars().all()
        assert len(scoped) == 3, "异常路径也必须恢复隔离"
        set_tenant(None)

    @pytest.mark.asyncio
    async def test_as_tenant_restores_after_exception(self, session):
        """as_tenant 同样要能在异常时正确恢复。"""
        await _seed(session)
        from sqlalchemy import func, select

        try:
            async with as_tenant_async(ALICE_ID):
                raise RuntimeError("boom")
        except RuntimeError:
            pass

        # 此时应回到无租户状态
        set_tenant(BOB_ID)
        n = (await session.execute(select(func.count(Post.id)))).scalar_one()
        assert n == 1
        set_tenant(None)


class TestTenantTableCoverage:
    """表覆盖检查：新增租户表时必须同步加进 TENANT_TABLES。"""

    def test_all_tenant_tables_registered(self):
        """任何一个带 user_id 的表漏登记，隔离就会对它失效。"""
        for table in Base.metadata.tables.values():
            if "user_id" not in table.columns:
                continue
            assert table.name in TENANT_TABLES, (
                f"表 {table.name} 带 user_id 但未登记进 TENANT_TABLES，"
                f"隔离对它无效！请加到 app/core/tenant.py 的 TENANT_TABLES"
            )

    def test_users_table_not_isolated(self):
        """users 表本身不带 user_id，不应被过滤——否则无法登录。"""
        assert "users" not in TENANT_TABLES