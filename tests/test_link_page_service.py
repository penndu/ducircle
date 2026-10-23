"""友链页服务的测试：入库、去重、增量检测。"""

from __future__ import annotations

import pytest
import pytest_asyncio

from app.core.security import site_hash
from app.models import LinkPage, Site, SiteSource, User

LINK_PAGE_HTML = """
<html><body>
  <div class="friend-list">
    <div class="card"><a href="https://alice.com">Alice</a></div>
    <div class="card"><a href="https://bob.com">Bob</a></div>
  </div>
</body></html>
"""

LINK_PAGE_HTML_MORE = """
<html><body>
  <div class="friend-list">
    <div class="card"><a href="https://alice.com">Alice</a></div>
    <div class="card"><a href="https://bob.com">Bob</a></div>
    <div class="card"><a href="https://carol.com">Carol</a></div>
  </div>
</body></html>
"""


class FakeResponse:
    def __init__(self, text: str) -> None:
        self.text = text
        self.content = text.encode("utf-8")
        self.encoding = "utf-8"
        self.reason_phrase = "OK"

    def raise_for_status(self) -> None:
        return None


class FakeClient:
    """假 HTTP 客户端，按顺序返回预置的页面。"""

    def __init__(self, pages: list[str]) -> None:
        self.pages = list(pages)
        self.requests: list[str] = []

    async def get(self, url: str) -> FakeResponse:
        self.requests.append(url)
        if not self.pages:
            raise AssertionError("请求次数超出预期")
        return FakeResponse(self.pages.pop(0))

    async def aclose(self) -> None:
        return None


@pytest_asyncio.fixture
async def session():
    """每个用例一个内存库，互不干扰。"""
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.models import Base

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


@pytest_asyncio.fixture
async def user(session):
    u = User(username="linkpage_user", password_hash="x" * 20)
    session.add(u)
    await session.commit()
    await session.refresh(u)
    return u


@pytest.fixture
async def link_page(session, user):
    from app.core.security import url_hash

    page = LinkPage(
        user_id=user.id,
        url="https://example.com/friends/",
        url_hash=url_hash("https://example.com/friends/"),
    )
    session.add(page)
    await session.commit()
    await session.refresh(page)
    return page


class TestScanLinkPage:
    @pytest.mark.asyncio
    async def test_adds_new_friends(self, session, user, link_page):
        """第一次扫描应该把页面上所有友链都加进来。"""
        from app.services.link_page_service import scan_link_page

        client = FakeClient([LINK_PAGE_HTML])
        result = await scan_link_page(session, user.id, link_page, client=client)

        assert result.total == 2
        assert result.added == 2
        assert result.error is None

        sites = (await session.execute(
            select_sites(user.id)
        )).scalars().all()
        assert len(sites) == 2
        assert {s.url for s in sites} == {"https://alice.com", "https://bob.com"}

    @pytest.mark.asyncio
    async def test_marks_source_and_discovered_from(self, session, user, link_page):
        """新加的友链要标记来源。"""
        from app.services.link_page_service import scan_link_page

        client = FakeClient([LINK_PAGE_HTML])
        await scan_link_page(session, user.id, link_page, client=client)

        sites = (await session.execute(select_sites(user.id))).scalars().all()
        for site in sites:
            assert site.source == SiteSource.LINK_PAGE.value
            assert site.discovered_from == link_page.url

    @pytest.mark.asyncio
    async def test_second_scan_skips_existing(self, session, user, link_page):
        """第二次扫描不应该重复添加。"""
        from app.services.link_page_service import scan_link_page

        client = FakeClient([LINK_PAGE_HTML, LINK_PAGE_HTML])
        await scan_link_page(session, user.id, link_page, client=client)
        result = await scan_link_page(session, user.id, link_page, client=client)

        assert result.total == 2
        assert result.added == 0
        assert result.skipped == 2

        sites = (await session.execute(select_sites(user.id))).scalars().all()
        assert len(sites) == 2

    @pytest.mark.asyncio
    async def test_detects_new_friend(self, session, user, link_page):
        """页面新增了一个友链，第二次扫描应该补进来。"""
        from app.services.link_page_service import scan_link_page

        client = FakeClient([LINK_PAGE_HTML, LINK_PAGE_HTML_MORE])
        await scan_link_page(session, user.id, link_page, client=client)
        result = await scan_link_page(session, user.id, link_page, client=client)

        assert result.total == 3
        assert result.added == 1
        assert result.skipped == 2

        sites = (await session.execute(select_sites(user.id))).scalars().all()
        assert len(sites) == 3
        assert any(s.url == "https://carol.com" for s in sites)

    @pytest.mark.asyncio
    async def test_does_not_touch_other_users_sites(self, session, user, link_page):
        """别的用户已有的友链不能被当成重复而跳过——但也不能被改动。"""
        from app.services.link_page_service import scan_link_page

        other = User(username="other_user", password_hash="y" * 20)
        session.add(other)
        await session.commit()
        await session.refresh(other)

        session.add(
            Site(
                user_id=other.id,
                name="别人的",
                url="https://alice.com",
                url_hash=site_hash("https://alice.com"),
            )
        )
        await session.commit()

        client = FakeClient([LINK_PAGE_HTML])
        result = await scan_link_page(session, user.id, link_page, client=client)

        # 同域名对不同用户是两条独立记录，各自都能加
        assert result.added == 2

        others = (await session.execute(select_sites(other.id))).scalars().all()
        assert len(others) == 1, "别人的友链不该被动"

    @pytest.mark.asyncio
    async def test_records_error_on_failure(self, session, user, link_page):
        """抓取失败要记录错误信息，不能静默。"""
        from app.services.link_page_service import LinkPageError, scan_link_page

        class FailingClient:
            async def get(self, url):
                raise LinkPageError("HTTP 404：Not Found")

            async def aclose(self):
                return None

        result = await scan_link_page(session, user.id, link_page, client=FailingClient())

        assert result.error is not None
        assert "404" in result.error
        await session.refresh(link_page)
        assert link_page.last_error is not None

    @pytest.mark.asyncio
    async def test_empty_page_reports_error(self, session, user, link_page):
        """页面里没友链要说明原因。"""
        from app.services.link_page_service import scan_link_page

        client = FakeClient(["<html><body>空的</body></html>"])
        result = await scan_link_page(session, user.id, link_page, client=client)

        assert result.total == 0
        assert result.error is not None
        assert "没有解析出友链" in result.error


def select_sites(user_id: int):
    from sqlalchemy import select

    return select(Site).where(Site.user_id == user_id)
