"""友链编辑功能与「用户填写的 RSS 地址真正生效」。

三件事：
1. 友链管理能改名称、网址、RSS 地址、备注
2. feed_url 填了之后，抓取时优先用它，且只用它
3. RSS 地址的末尾斜杠必须原样保留——用户就是这么填的
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


async def _register(client, username="alice"):
    return await client.post(
        "/web/register",
        data={"username": username, "password": "password123", "email": ""},
        follow_redirects=False,
    )


async def _add_site(client, url="https://example.com", name="示例"):
    r = await client.post(
        "/api/sites",
        json={"name": name, "url": url},
    )
    assert r.status_code == 201, r.text
    return r.json()


class TestSiteEditing:
    @pytest.mark.asyncio
    async def test_patch_updates_url(self, client):
        await _register(client)
        site = await _add_site(client)

        r = await client.patch(
            f"/api/sites/{site['id']}",
            json={"url": "https://blog.example.com"},
        )
        assert r.status_code == 200, r.text
        # 站点网址走去重键那套（normalize_url），根路径会补上尾斜杠
        assert r.json()["url"] == "https://blog.example.com/"

    @pytest.mark.asyncio
    async def test_patch_updates_name_and_note(self, client):
        await _register(client)
        site = await _add_site(client)

        r = await client.patch(
            f"/api/sites/{site['id']}",
            json={"name": "新名字", "note": "改个备注"},
        )
        assert r.status_code == 200
        data = r.json()
        assert data["name"] == "新名字"
        assert data["note"] == "改个备注"

    @pytest.mark.asyncio
    async def test_patch_sets_feed_url(self, client):
        await _register(client)
        site = await _add_site(client)

        r = await client.patch(
            f"/api/sites/{site['id']}",
            json={"feed_url": "https://example.com/atom.xml"},
        )
        assert r.status_code == 200
        assert r.json()["feed_url"] == "https://example.com/atom.xml"

    @pytest.mark.asyncio
    async def test_patch_clears_feed_url(self, client):
        """空字符串表示清空 RSS 地址。"""
        await _register(client)
        site = await _add_site(client)
        await client.patch(
            f"/api/sites/{site['id']}",
            json={"feed_url": "https://example.com/feed/"},
        )

        r = await client.patch(
            f"/api/sites/{site['id']}", json={"feed_url": ""}
        )
        assert r.status_code == 200
        assert r.json()["feed_url"] is None

    @pytest.mark.asyncio
    async def test_feed_url_keeps_trailing_slash(self, client):
        """末尾斜杠必须原样保留。

        /feed 和 /feed/ 在不少站点上是两个地址，
        少数自建 CMS 只认带斜杠的。用户填了就照他的意思。
        """
        await _register(client)
        site = await _add_site(client)

        r = await client.patch(
            f"/api/sites/{site['id']}",
            json={"feed_url": "https://example.com/feed/"},
        )
        assert r.status_code == 200
        assert r.json()["feed_url"] == "https://example.com/feed/"

    @pytest.mark.asyncio
    async def test_feed_url_gets_scheme(self, client):
        """没写 https:// 要自动补。"""
        await _register(client)
        site = await _add_site(client)

        r = await client.patch(
            f"/api/sites/{site['id']}",
            json={"feed_url": "example.com/atom.xml"},
        )
        assert r.status_code == 200
        assert r.json()["feed_url"] == "https://example.com/atom.xml"

    @pytest.mark.asyncio
    async def test_create_with_feed_url(self, client):
        """添加友链时也能直接填 RSS 地址。"""
        await _register(client)
        r = await client.post(
            "/api/sites",
            json={
                "name": "带 RSS",
                "url": "https://rss.example.com",
                "feed_url": "https://rss.example.com/feed/",
            },
        )
        assert r.status_code == 201
        assert r.json()["feed_url"] == "https://rss.example.com/feed/"

    @pytest.mark.asyncio
    async def test_sites_page_has_edit_ui(self, client):
        """友链管理页要有编辑入口和 RSS 地址输入框。"""
        await _register(client)
        await _add_site(client)
        html = (await client.get("/web/sites")).text
        assert "openEdit(" in html
        assert "edit-modal" in html
        assert "edit-feed-url" in html
        assert "add-feed-url" in html


class TestFeedUrlActuallyUsed:
    @pytest.mark.asyncio
    async def test_manual_feed_url_is_used_exclusively(self, monkeypatch):
        """填了 feed_url 就只用它，不再自动猜。"""
        from app.services.crawler import Crawler

        tried: list[str] = []

        async def _fake_fetch(self, url, *, stream=False):
            tried.append(url)
            return 200, (
                '<?xml version="1.0"?><rss version="2.0"><channel>'
                "<title>T</title><item><title>一</title>"
                "<link>https://example.com/1</link></item></channel></rss>"
            )

        monkeypatch.setattr(Crawler, "_fetch", _fake_fetch)

        async with Crawler() as crawler:
            result = await crawler.crawl_site(
                "https://example.com", feed_url="https://example.com/atom.xml"
            )

        assert result.status == "success"
        # robots.txt 是抓取前的常规动作，不算「乱试别的源」
        feeds = [u for u in tried if "robots.txt" not in u]
        assert feeds == ["https://example.com/atom.xml"], (
            f"填了 feed_url 还去试了别的：{feeds}"
        )

    @pytest.mark.asyncio
    async def test_no_manual_feed_url_uses_discovery(self, monkeypatch):
        """没填就照常自动发现。"""
        from app.services.crawler import Crawler

        tried: list[str] = []

        async def _fake_fetch(self, url, *, stream=False):
            tried.append(url)
            return 200, "<html><body>nothing</body></html>"

        monkeypatch.setattr(Crawler, "_fetch", _fake_fetch)

        async with Crawler() as crawler:
            await crawler.crawl_site("https://example.com")

        feeds = [u for u in tried if "robots.txt" not in u]
        assert feeds, "应该试了若干 feed 候选路径"
        assert any("/feed" in u for u in feeds)
        assert any("/atom.xml" in u for u in feeds)

    @pytest.mark.asyncio
    async def test_manual_feed_url_failure_is_reported(self, monkeypatch):
        """填了但抓不通，要明确报错，不能悄悄换成别的源。"""
        from app.services.crawler import Crawler

        async def _fake_fetch(self, url, *, stream=False):
            return 404, "not found"

        monkeypatch.setattr(Crawler, "_fetch", _fake_fetch)

        async with Crawler() as crawler:
            result = await crawler.crawl_site(
                "https://example.com", feed_url="https://example.com/atom.xml"
            )

        assert result.status == "failed"


class TestFeedPathVariants:
    def test_feed_paths_include_slash_and_atom_variants(self):
        """候选路径要覆盖带斜杠的 /feed/ 和 /atom.xml。"""
        from app.parser.feed import COMMON_FEED_PATHS

        assert "/feed" in COMMON_FEED_PATHS
        assert "/feed/" in COMMON_FEED_PATHS
        assert "/atom.xml" in COMMON_FEED_PATHS
        assert "/rss.xml" in COMMON_FEED_PATHS

    def test_request_url_preserves_slash_for_feed_probe(self):
        """探测 feed 时拼出来的地址要保留斜杠。"""
        from app.core.security import request_url
        from urllib.parse import urljoin

        joined = urljoin("https://example.com", "/feed/")
        assert request_url(joined) == "https://example.com/feed/"


class TestWwwFallback:
    """裸域连不上时要自动试 www.。

    存量数据里站点网址存的是剥掉 www 的裸域（去重键形态），
    而很多国内博客只在 www 上配了 DNS。
    不做回退，iczrx.cn 这类站点永远连不上。
    """

    def test_with_www_adds_prefix(self):
        from app.services.crawler import _with_www

        assert _with_www("https://iczrx.cn/") == "https://www.iczrx.cn/"
        assert _with_www("http://example.com/p") == "http://www.example.com/p"

    def test_with_www_skips_existing_www(self):
        from app.services.crawler import _with_www

        assert _with_www("https://www.iczrx.cn/") is None

    def test_with_www_skips_subdomains(self):
        """blog.x.com 加 www 没意义。"""
        from app.services.crawler import _with_www

        assert _with_www("https://blog.gxres.net") is None
        assert _with_www("https://a.b.c.com/x") is None

    def test_with_www_keeps_port(self):
        from app.services.crawler import _with_www

        assert _with_www("http://example.com:8080/p") == (
            "http://www.example.com:8080/p"
        )

    @pytest.mark.asyncio
    async def test_crawl_falls_back_to_www(self, monkeypatch):
        """裸域抛连接错误时，自动改用 www. 重试。"""
        import httpx

        from app.services.crawler import Crawler

        tried: list[str] = []

        async def _fake_fetch_final_url(self, url):
            tried.append(url)
            if "www." not in url:
                raise httpx.ConnectError("bare domain unreachable")
            return 200, "<html><body>ok</body></html>", url

        monkeypatch.setattr(Crawler, "_fetch_final_url", _fake_fetch_final_url)

        async def _fake_robots(self, url):
            return True

        monkeypatch.setattr(Crawler, "_respect_robots", _fake_robots)

        async with Crawler() as crawler:
            result = await crawler.crawl_site("https://iczrx.cn/")

        assert tried == ["https://iczrx.cn/", "https://www.iczrx.cn/"], (
            f"没有回退到 www：{tried}"
        )
        assert result.status == "success"


class TestSiteUrlKeepsWww:
    """站点网址存下来要保留 www.。

    之前存的是 normalize_url 的结果（剥掉 www.），
    结果用户填 https://www.iczrx.cn，存进去变成 https://iczrx.cn/，
    抓取时连不上。编辑功能等于白做。
    """

    @pytest.mark.asyncio
    async def test_create_keeps_www(self, client):
        await _register(client)
        r = await client.post(
            "/api/sites",
            json={"name": "带 www", "url": "https://www.iczrx.cn"},
        )
        assert r.status_code == 201
        assert r.json()["url"] == "https://www.iczrx.cn/"

    @pytest.mark.asyncio
    async def test_patch_keeps_www(self, client):
        await _register(client)
        site = await _add_site(client)
        r = await client.patch(
            f"/api/sites/{site['id']}",
            json={"url": "https://www.iczrx.cn"},
        )
        assert r.status_code == 200
        assert r.json()["url"] == "https://www.iczrx.cn/"

    @pytest.mark.asyncio
    async def test_url_hash_still_dedupes_www_and_bare(self, client):
        """去重不受影响：www 与裸域仍算同一个站。"""
        await _register(client)
        await _add_site(client, url="https://www.iczrx.cn", name="A")

        r = await client.post(
            "/api/sites",
            json={"name": "B", "url": "https://iczrx.cn"},
        )
        assert r.status_code == 409, "裸域与 www 应该被认作同一个站点"


class TestDetachedSiteCarriesFeedUrl:
    """_DetachedSite 是脱离 ORM 会话的快照，必须带上 feed_url。

    漏了的话，抓取时访问 site.feed_url 会抛
    AttributeError: '_DetachedSite' object has no attribute 'feed_url'，
    表现为「手动点抓取就报内部错误」。
    """

    def test_detached_site_has_feed_url_slot(self):
        from app.services.crawl_service import _DetachedSite

        assert "feed_url" in _DetachedSite.__slots__
        site = _DetachedSite(1, "n", "https://x.com", 1, "https://x.com/feed/")
        assert site.feed_url == "https://x.com/feed/"

    def test_detached_site_feed_url_defaults_to_none(self):
        from app.services.crawl_service import _DetachedSite

        site = _DetachedSite(1, "n", "https://x.com", 1)
        assert site.feed_url is None

    def test_crawler_tolerates_missing_feed_url_attr(self):
        """传入没有 feed_url 属性的对象也不能炸。"""
        import inspect

        from app.services.crawler import Crawler

        src = inspect.getsource(Crawler.crawl_site_with_loaded_rules)
        assert 'getattr(site, "feed_url", None)' in src, (
            "应该用 getattr 兜底，防止对象缺 feed_url 属性时炸掉"
        )
