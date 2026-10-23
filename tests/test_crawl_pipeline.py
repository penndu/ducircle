"""抓取流水线与调度器测试。

重点验证三件事：
1. 抓取结果能正确落库（去重、策略记录、日志写入）
2. 失败站点会被标记并降频，不会无限重试
3. 多租户在流水线里仍然隔离——A 用户调度器不能抓到 B 用户的数据
"""

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool


@pytest_asyncio.fixture
async def db():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    from app.models import Base

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


def make_result(posts, status="success", strategy="feed", error=None):
    """构造一个抓取结果，避免测试真的联网。"""
    from app.parser.base import CrawlResult, ParsedPost, ParsedSite

    site = ParsedSite(
        name="测试站",
        url="https://test.example.com",
        strategy=strategy,
        posts=[
            ParsedPost(title=t, link=f"https://test.example.com/{slug}",
                       strategy=strategy)
            for t, slug in posts
        ],
    )
    return CrawlResult(
        url="https://test.example.com",
        status=status,
        strategy=strategy if status == "success" else None,
        error=error,
        post_count=len(posts),
        site=site if status == "success" else None,
    )


@pytest.mark.asyncio
async def test_save_result_persists_posts(db):
    from app.core.tenant import set_tenant
    from app.core.security import site_hash
    from app.models import Site
    from app.services.crawler import save_crawl_result

    set_tenant(1)
    db.add(Site(id=1, user_id=1, name="测试站", url="https://test.example.com",
                url_hash=site_hash("https://test.example.com"), source="manual"))
    await db.commit()

    result = make_result([("文章一", "a"), ("文章二", "b")])
    new_posts = await save_crawl_result(db, await db.get(Site, 1), result)

    assert new_posts == 2
    await db.commit()

    from sqlalchemy import select

    from app.models import Post

    posts = (await db.execute(select(Post))).scalars().all()
    assert len(posts) == 2
    assert {p.title for p in posts} == {"文章一", "文章二"}
    assert all(p.strategy == "feed" for p in posts)

    # 站点状态应为成功
    site = await db.get(Site, 1)
    assert site.error is False
    assert site.strategy == "feed"
    assert site.last_success_at is not None


@pytest.mark.asyncio
async def test_duplicate_posts_not_inserted_twice(db):
    """同一批文章重复抓取不应产生重复记录。"""
    from app.core.security import site_hash
    from app.core.tenant import set_tenant
    from app.models import Post, Site
    from app.services.crawler import save_crawl_result
    from sqlalchemy import func, select

    set_tenant(1)
    db.add(Site(id=1, user_id=1, name="测试站", url="https://test.example.com",
                url_hash=site_hash("https://test.example.com"), source="manual"))
    await db.commit()
    site = await db.get(Site, 1)

    await save_crawl_result(db, site, make_result([("文章一", "a")]))
    await db.commit()
    second = await save_crawl_result(db, site, make_result([("文章一", "a")]))

    assert second == 0, "重复抓取不应新增文章"
    count = (await db.execute(select(func.count(Post.id)))).scalar_one()
    assert count == 1


@pytest.mark.asyncio
async def test_url_variation_deduped(db):
    """带 utm 参数或 www 前缀的同一篇文章应被识别为重复。"""
    from app.core.security import site_hash
    from app.core.tenant import set_tenant
    from app.models import Post, Site
    from app.services.crawler import save_crawl_result
    from sqlalchemy import func, select

    set_tenant(1)
    db.add(Site(id=1, user_id=1, name="测试站", url="https://test.example.com",
                url_hash=site_hash("https://test.example.com"), source="manual"))
    await db.commit()
    site = await db.get(Site, 1)

    from app.parser.base import CrawlResult, ParsedPost, ParsedSite

    def make(url):
        s = ParsedSite(url="https://test.example.com", strategy="feed",
                       posts=[ParsedPost(title="同一篇", link=url, strategy="feed")])
        return CrawlResult(url=s.url, status="success", strategy="feed",
                           post_count=1, site=s)

    await save_crawl_result(db, site, make("https://test.example.com/post-a"))
    await db.commit()
    added = await save_crawl_result(
        db, site, make("https://www.test.example.com/post-a?utm_source=x")
    )
    await db.commit()

    assert added == 0, "带追踪参数的同一篇文章应被去重"
    count = (await db.execute(select(func.count(Post.id)))).scalar_one()
    assert count == 1


@pytest.mark.asyncio
async def test_failure_marks_site_and_backs_off(db):
    """抓取失败要标记站点、累计失败次数并逐步降频。"""
    from app.core.security import site_hash
    from app.core.tenant import set_tenant
    from app.models import Site
    from app.services.crawler import save_crawl_result

    set_tenant(1)
    db.add(Site(id=1, user_id=1, name="挂了", url="https://down.example.com",
                url_hash=site_hash("https://down.example.com"), source="manual"))
    await db.commit()

    site = await db.get(Site, 1)
    original_interval = site.crawl_interval_hours

    for i in range(6):
        result = make_result([], status="failed", error="HTTP 404")
        await save_crawl_result(db, site, result)
        await db.commit()
        site = await db.get(Site, 1)

    assert site.error is True
    assert site.error_message == "HTTP 404"
    assert site.consecutive_failures == 6
    # 连续失败后必须延长间隔，不能一直高频重试
    assert site.crawl_interval_hours > original_interval
    assert site.last_success_at is None


@pytest.mark.asyncio
async def test_failure_backoff_capped(db):
    """降频间隔必须有上限，否则一次失败会永久放弃这个站点。"""
    from app.core.security import site_hash
    from app.core.tenant import set_tenant
    from app.models import Site
    from app.services.crawler import save_crawl_result

    set_tenant(1)
    db.add(Site(id=1, user_id=1, name="挂了", url="https://down.example.com",
                url_hash=site_hash("https://down.example.com"), source="manual"))
    await db.commit()
    site = await db.get(Site, 1)

    for _ in range(30):
        await save_crawl_result(db, site, make_result([], status="failed"))
        site = await db.get(Site, 1)

    assert site.crawl_interval_hours <= 24 * 7, "降频间隔上限为 7 天"


@pytest.mark.asyncio
async def test_recovery_resets_failure_state(db):
    """站点恢复后失败计数和频率都要重置。"""
    from app.core.security import site_hash
    from app.core.tenant import set_tenant
    from app.models import Site
    from app.services.crawler import save_crawl_result

    set_tenant(1)
    db.add(Site(id=1, user_id=1, name="站", url="https://ok.example.com",
                url_hash=site_hash("https://ok.example.com"), source="manual"))
    await db.commit()
    site = await db.get(Site, 1)

    for _ in range(5):
        await save_crawl_result(db, site, make_result([], status="failed"))
    refreshed = await db.get(Site, 1)
    assert refreshed.consecutive_failures == 5

    await save_crawl_result(db, site, make_result([("回来了", "back")]))
    await db.commit()

    site = await db.get(Site, 1)
    assert site.error is False
    assert site.consecutive_failures == 0
    assert site.crawl_interval_hours == 12


@pytest.mark.asyncio
async def test_crawl_log_written(db):
    """每次抓取都要留日志——这是后台「抓取失败列表」的数据来源。"""
    from app.core.security import site_hash
    from app.core.tenant import set_tenant
    from app.models import CrawlLog, Site
    from app.services.crawler import save_crawl_result
    from sqlalchemy import select

    set_tenant(1)
    db.add(Site(id=1, user_id=1, name="站", url="https://log.example.com",
                url_hash=site_hash("https://log.example.com"), source="manual"))
    await db.commit()

    await save_crawl_result(db, await db.get(Site, 1), make_result([("一", "a")]))
    await save_crawl_result(
        db, await db.get(Site, 1), make_result([], status="failed", error="超时")
    )
    await db.commit()

    logs = (await db.execute(select(CrawlLog))).scalars().all()
    assert len(logs) == 2
    statuses = {log.status for log in logs}
    assert statuses == {"success", "failed"}
    failed = next(log for log in logs if log.status == "failed")
    assert failed.error == "超时"


@pytest.mark.asyncio
async def test_tenant_isolation_in_pipeline(db):
    """流水线不能跨用户写入。这是调度器最容易出错的点。"""
    from sqlalchemy import select

    from app.core.security import site_hash
    from app.core.tenant import as_tenant_async, set_tenant
    from app.models import Post, Site
    from app.services.crawler import save_crawl_result

    set_tenant(1)
    db.add(Site(id=1, user_id=1, name="Alice的站",
                url="https://alice.example.com",
                url_hash=site_hash("https://alice.example.com"), source="manual"))
    await db.commit()

    alice_site = await db.get(Site, 1)
    async with as_tenant_async(1):
        await save_crawl_result(db, alice_site, make_result([("A文", "a")]))
    await db.commit()

    # 切到 Bob 上下文：应该看不到 Alice 的站点
    async with as_tenant_async(2):
        visible = (await db.execute(select(Site))).scalars().all()
        assert visible == [], "Bob 不应看到 Alice 的站点"

        posts = (await db.execute(select(Post))).scalars().all()
        assert posts == [], "Bob 不应看到 Alice 的文章"

    set_tenant(None)


@pytest.mark.asyncio
async def test_should_crawl_respects_interval(db):
    """should_crawl 按间隔判断，避免重复打扰对方服务器。"""
    from datetime import datetime, timedelta

    from app.models import Site

    site = Site(user_id=1, name="x", url="https://x.com", url_hash="h", source="manual")

    # 从未抓过 → 该抓
    assert should_crawl(site) is True

    # 刚抓过 → 不该抓
    site.last_crawled_at = datetime.now()
    assert should_crawl(site) is False

    # 超过间隔 → 该抓
    site.last_crawled_at = datetime.now() - timedelta(hours=13)
    assert should_crawl(site) is True

    # 停用的站点永远不抓
    site.active = False
    assert should_crawl(site) is False


from app.services.crawler import should_crawl  # noqa: E402