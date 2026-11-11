"""抓取失败页的「排查建议」文案。

之前那里的 RSS 地址是直接字符串拼接的：
    {{ site.url }}atom.xml
站点地址末尾没有斜杠时，会拼出「https://vjo.ccatom.xml」——
少一个斜杠，地址完全不对，用户照着访问必然 404。
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


class TestUrlJoinFilter:
    def test_adds_missing_slash(self):
        """站点地址没有末尾斜杠时，拼接必须补上。"""
        from app.web import templates

        f = templates.env.filters["urljoin"]
        assert f("https://vjo.cc", "/atom.xml") == "https://vjo.cc/atom.xml"
        assert f("https://vjo.cc", "/feed/") == "https://vjo.cc/feed/"

    def test_keeps_correct_slash(self):
        """站点地址已有末尾斜杠时不能重复加。"""
        from app.web import templates

        f = templates.env.filters["urljoin"]
        assert f("https://vjo.cc/", "/atom.xml") == "https://vjo.cc/atom.xml"
        assert f("https://www.iczrx.cn/", "/feed/") == "https://www.iczrx.cn/feed/"

    def test_empty_base(self):
        from app.web import templates

        f = templates.env.filters["urljoin"]
        assert f("", "/feed/") == "/feed/"


class TestFailedPageTips:
    @pytest.mark.asyncio
    async def test_tips_use_correct_feed_urls(self, client):
        """排查建议里的 RSS 地址必须带斜杠且拼接正确。"""
        r = await client.post(
            "/web/register",
            data={"username": "alice", "password": "password123", "email": ""},
            follow_redirects=False,
        )
        assert r.status_code in (200, 303, 302), r.text
        r = await client.post(
            "/api/sites",
            json={"name": "刘郎阁", "url": "https://vjo.cc"},
        )
        assert r.status_code == 201, r.text

        # 把站点标记为失败，才会出现在这个页面上
        await _mark_site_failed(client, r.json()["id"])

        html = (await client.get("/web/failed")).text
        # 拼接错误的话会出现 vjo.ccatom.xml 这种
        assert "vjo.ccatom.xml" not in html, "拼接漏了斜杠"
        assert "https://vjo.cc/atom.xml" in html
        assert "https://vjo.cc/feed/" in html

    def test_no_raw_concatenation_in_template(self):
        """模板里不能再出现裸拼接。"""
        from pathlib import Path

        content = Path("web/templates/failed.html").read_text(encoding="utf-8")
        assert "{{ site.url }}atom.xml" not in content
        assert "{{ site.url }}feed" not in content
        assert "urljoin" in content

    @pytest.mark.asyncio
    async def test_tips_mention_filling_feed_url(self, client):
        """建议里要告诉用户下一步做什么——填 RSS 地址。"""
        await client.post(
            "/web/register",
            data={"username": "alice", "password": "password123", "email": ""},
            follow_redirects=False,
        )
        r = await client.post(
            "/api/sites",
            json={"name": "刘郎阁", "url": "https://vjo.cc"},
        )
        assert r.status_code == 201, r.text
        await _mark_site_failed(client, r.json()["id"])

        html = (await client.get("/web/failed")).text
        assert "排查建议" in html
        assert "RSS 地址" in html


async def _mark_site_failed(client, site_id: int) -> None:
    """把站点标记为抓取失败。

    直接改数据库——没有 API 能设置 error 标志，
    而这个标志是失败页的筛选条件。
    """
    from app.db import get_session_factory
    from sqlalchemy import update

    from app.models import Site

    factory = get_session_factory()
    async with factory() as db:
        await db.execute(
            update(Site)
            .where(Site.id == site_id)
            .values(error=True, error_message="测试用")
        )
        await db.commit()
