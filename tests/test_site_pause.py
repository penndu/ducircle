"""连续失败达阈值后暂停采集。

背景：有些站点彻底挂了（域名过期、服务器下线）。
原有的「连续失败 5 次降频」只是少来几次，还是每 7 天骚扰一次，
永远失败还污染失败列表。

新规则：连续失败 10 次 → 标记 paused，定时抓取和批量抓取都跳过，
必须用户手动点「恢复采集」才重新开始。
"""

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool


@pytest_asyncio.fixture
async def client(monkeypatch):
    import app.db as db_module
    from app.models import Base

    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(db_module, "get_session_factory", lambda: factory)

    async def _check_db():
        return True

    monkeypatch.setattr(db_module, "check_db", _check_db)

    from app.core.ratelimit import RateLimiter

    async def _allow(self, user_id, cost=1.0, rate=None, burst=None):
        return True

    monkeypatch.setattr(RateLimiter, "allow", _allow)

    from app.main import app as fastapi_app

    transport = ASGITransport(app=fastapi_app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    await engine.dispose()


class TestPauseThreshold:
    def test_threshold_is_ten(self):
        from app.services.crawler import PAUSE_AFTER_FAILURES

        assert PAUSE_AFTER_FAILURES == 10

    def test_threshold_differs_from_degrade(self):
        """降频阈值（5）和暂停阈值（10）必须是两个不同的数。"""
        from app.services.crawler import FAILURE_THRESHOLD, PAUSE_AFTER_FAILURES

        assert FAILURE_THRESHOLD < PAUSE_AFTER_FAILURES


class TestPauseOnFailure:
    @pytest.mark.asyncio
    async def test_paused_after_ten_failures(self):
        """连续失败 10 次后应标记暂停。"""
        from app.models import Base, Site, User
        from app.parser.base import CrawlResult
        from app.services.crawler import save_crawl_result
        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
        from sqlalchemy.pool import StaticPool

        engine = create_async_engine(
            "sqlite+aiosqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(engine, expire_on_commit=False)

        async with factory() as db:
            user = User(username="u1", password_hash="x")
            db.add(user)
            await db.flush()
            site = Site(user_id=user.id, name="s", url="https://dead.com",
                        url_hash="h")
            db.add(site)
            await db.flush()

            failed = CrawlResult(
                url="https://dead.com",
                status="failed",
                error="ConnectTimeout: 连不上",
            )

            for i in range(9):
                await save_crawl_result(db, site, failed)
                await db.commit()
                assert not site.paused, f"第 {i + 1} 次失败不该暂停"

            await save_crawl_result(db, site, failed)
            await db.commit()

            assert site.paused, "第 10 次失败应暂停"
            assert site.paused_at is not None
            assert "10" in site.paused_reason

        await engine.dispose()

    @pytest.mark.asyncio
    async def test_success_clears_pause(self):
        """抓取成功了就不该再暂停——用户可能已经修好了。"""
        from app.models import Base, Site, User
        from app.parser.base import CrawlResult, ParsedPost, ParsedSite
        from app.services.crawler import save_crawl_result
        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
        from sqlalchemy.pool import StaticPool

        engine = create_async_engine(
            "sqlite+aiosqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(engine, expire_on_commit=False)

        async with factory() as db:
            user = User(username="u1", password_hash="x")
            db.add(user)
            await db.flush()
            site = Site(user_id=user.id, name="s", url="https://x.com",
                        url_hash="h")
            db.add(site)
            await db.flush()

            failed = CrawlResult(url="https://x.com", status="failed", error="e")
            for _ in range(10):
                await save_crawl_result(db, site, failed)
                await db.commit()
            assert site.paused

            ok = CrawlResult(
                url="https://x.com",
                status="success",
                strategy="feed",
                site=ParsedSite(
                    url="https://x.com",
                    name="s",
                    strategy="feed",
                    posts=[ParsedPost(title="一", link="https://x.com/p/1/")],
                ),
            )
            await save_crawl_result(db, site, ok)
            await db.commit()

            assert not site.paused, "成功后应解除暂停"
            assert site.paused_at is None
            assert site.paused_reason is None
            assert site.consecutive_failures == 0

        await engine.dispose()


class TestPausedSkippedByCrawl:
    @pytest.mark.asyncio
    async def test_paused_site_not_crawled(self):
        """暂停的站点不该被抓。"""
        import inspect

        from app.services.crawl_service import _crawl_user_sites

        src = inspect.getsource(_crawl_user_sites)
        assert "Site.paused.is_(False)" in src, (
            "抓取站点时要过滤掉 paused 的"
        )


class TestResumeApi:
    @pytest.mark.asyncio
    async def test_resume_clears_pause(self, client):
        await client.post(
            "/web/register",
            data={"username": "alice", "password": "password123", "email": ""},
            follow_redirects=False,
        )
        r = await client.post(
            "/api/sites", json={"name": "测试站", "url": "https://x.com"}
        )
        assert r.status_code == 201, r.text
        site_id = r.json()["id"]

        # 先暂停
        r = await client.post(f"/api/sites/{site_id}/pause")
        assert r.status_code == 200
        assert r.json()["paused"] is True
        assert r.json()["paused_reason"]

        # 再恢复
        r = await client.post(f"/api/sites/{site_id}/resume")
        assert r.status_code == 200
        data = r.json()
        assert data["paused"] is False
        assert data["paused_at"] is None
        assert data["paused_reason"] is None
        assert data["consecutive_failures"] == 0

    @pytest.mark.asyncio
    async def test_resume_unknown_site_404(self, client):
        await client.post(
            "/web/register",
            data={"username": "alice", "password": "password123", "email": ""},
            follow_redirects=False,
        )
        r = await client.post("/api/sites/999999/resume")
        assert r.status_code == 404

    @pytest.mark.asyncio
    async def test_site_out_includes_pause_fields(self, client):
        await client.post(
            "/web/register",
            data={"username": "alice", "password": "password123", "email": ""},
            follow_redirects=False,
        )
        r = await client.post(
            "/api/sites", json={"name": "测试站", "url": "https://x.com"}
        )
        data = r.json()
        assert "paused" in data
        assert "paused_at" in data
        assert "paused_reason" in data


class TestPauseUi:
    def test_failed_page_shows_resume(self):
        from pathlib import Path

        content = Path("web/templates/failed.html").read_text(encoding="utf-8")
        assert "已暂停采集" in content
        assert "resumeSite" in content
        assert "/resume" in content

    def test_sites_page_shows_pause_badge(self):
        from pathlib import Path

        content = Path("web/templates/sites.html").read_text(encoding="utf-8")
        assert "已暂停采集" in content
        assert "resumeSite" in content

    def test_model_has_paused_fields(self):
        from app.models.site import Site

        cols = Site.__table__.columns
        assert "paused" in cols
        assert "paused_at" in cols
        assert "paused_reason" in cols
