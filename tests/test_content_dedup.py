"""跨永久链接的重复文章去重。

实测案例（obaby@mars / zhongxiaojie.com）：
同一篇文章被采了三次——
  id=206  https://zhongxiaojie.com/2026/10/2281/   旧日期式永久链接
  id=368  https://zhongxiaojie.com/post/2281/       改成 post 永久链接后
  id=480  https://zhongxiaojie.cn/post/2281/        feed 里域名变成 .cn

三者 URL 不同 → guid_hash 不同 → 当成三篇入库。

修法：content_hash，基于 RSS 的 guid（WordPress 是 ?p=2281 这种文章 ID），
不随永久链接结构或域名变化。指纹带 site_id，避免不同站点的相同 ID 撞车。
"""

import pytest


class TestExtractGuid:
    def test_wordpress_p_id(self):
        """WordPress 的 ?p=12345 形态 → 归一成 wp:12345。"""
        from app.parser.feed import _extract_guid

        guid = _extract_guid(
            {"id": "https://zhongxiaojie.cn/?p=2281"},
            "https://zhongxiaojie.com/post/2281/",
        )
        assert guid == "wp:2281"

    def test_same_as_link_returns_none(self):
        """guid 就是 permalink 本身时没有额外价值，回退按 link 去重。"""
        from app.parser.feed import _extract_guid

        guid = _extract_guid(
            {"id": "https://example.com/post/1/"},
            "https://example.com/post/1/",
        )
        assert guid is None

    def test_no_guid_returns_none(self):
        from app.parser.feed import _extract_guid

        assert _extract_guid({}, "https://example.com/post/1/") is None
        assert _extract_guid({"id": ""}, "https://example.com/post/1/") is None

    def test_other_guid_kept(self):
        """非 WordPress 形态的 guid 原样保留。"""
        from app.parser.feed import _extract_guid

        guid = _extract_guid(
            {"id": "urn:uuid:12345678"},
            "https://example.com/post/1/",
        )
        assert guid == "urn:uuid:12345678"

    def test_strip_scheme_ignores_www_and_slash(self):
        from app.parser.feed import _strip_scheme

        assert _strip_scheme("https://www.a.com/x/") == "a.com/x"
        assert _strip_scheme("http://a.com/x") == "a.com/x"


class TestContentHash:
    def test_none_guid_returns_none(self):
        from app.core.security import content_hash

        assert content_hash(None, site_id=1) is None
        assert content_hash("", site_id=1) is None

    def test_same_guid_same_hash(self):
        from app.core.security import content_hash

        a = content_hash("wp:2281", site_id=58)
        b = content_hash("wp:2281", site_id=58)
        assert a == b

    def test_different_site_different_hash(self):
        """不同站点的相同文章 ID 不能撞车。"""
        from app.core.security import content_hash

        assert content_hash("wp:2281", site_id=58) != content_hash(
            "wp:2281", site_id=99
        )

    def test_guid_hash_unchanged(self):
        """guid_hash 的算法不能动——历史数据按它算的。"""
        from app.core.security import guid_hash, normalize_url

        url = "https://zhongxiaojie.com/2026/10/2281/"
        import hashlib

        expected = hashlib.sha1(normalize_url(url).encode()).hexdigest()
        assert guid_hash(url) == expected

    def test_migration_hash_matches_runtime(self):
        """迁移回填的指纹必须和运行时算的一致，否则去重等于没做。"""
        import hashlib

        from app.core.security import content_hash

        mig = hashlib.sha1(b"site:58:id:wp:2281").hexdigest()
        assert content_hash("wp:2281", site_id=58) == mig


class TestMigrationHelpers:
    def test_article_id_extracted(self):
        from importlib import import_module

        mod = import_module(
            "alembic.versions.0005_post_content_hash".replace(
                "alembic.versions.", "alembic.versions."
            )
        ) if False else None
        # 直接用文件里的实现，避免 alembic 目录不是包的问题
        import importlib.util
        from pathlib import Path

        path = Path("alembic/versions/0005_post_content_hash.py")
        spec = importlib.util.spec_from_file_location("mig005", path)
        mig = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mig)

        assert mig._article_id("https://a.com/post/2281/") == "2281"
        assert mig._article_id("https://a.com/2026/10/2281/") == "2281"
        assert mig._article_id("https://a.com/hello-world/") is None
        # 太短的数字更可能是分页，不取
        assert mig._article_id("https://a.com/page/1/") is None
        assert mig._article_id("https://a.com/") is None


class TestCrawlerDedup:
    @pytest.mark.asyncio
    async def test_same_article_different_links_deduped(self, monkeypatch):
        """同一篇文章的不同链接形态，第二条应该被识别为已存在。"""
        from app.models import Base, Post, Site, User
        from app.services.crawler import save_crawl_result
        from app.parser.base import ParsedPost, ParsedSite
        from app.parser.base import CrawlResult
        from sqlalchemy import select
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
            site = Site(user_id=user.id, name="s", url="https://a.com",
                        url_hash="h1")
            db.add(site)
            await db.flush()

            def _result(link):
                return CrawlResult(
                    url="https://a.com",
                    status="success",
                    strategy="feed",
                    site=ParsedSite(
                        url="https://a.com",
                        name="s",
                        strategy="feed",
                        posts=[
                            ParsedPost(
                                title="狂欢",
                                link=link,
                                strategy="feed",
                                guid="wp:2281",
                            )
                        ],
                    ),
                )

            # 第一次：/post/2281/
            n1 = await save_crawl_result(db, site, _result("https://a.com/post/2281/"))
            await db.commit()
            # 第二次：换了域名和永久链接结构
            n2 = await save_crawl_result(
                db, site, _result("https://a.cn/2026/10/2281/")
            )
            await db.commit()

            assert n1 == 1, "第一次应该新增 1 篇"
            assert n2 == 0, f"第二次不该新增，实际新增 {n2}"

            rows = (
                await db.execute(
                    select(Post).where(Post.site_id == site.id)
                )
            ).scalars().all()
            assert len(rows) == 1, f"应该只有 1 篇，实际 {len(rows)}"

        await engine.dispose()


class TestNewPostsCounting:
    """「本次抓到几篇」和「真正新增几篇」必须分开。

    混用的后果：重复抓取时抓到 10 篇但一篇没新增，
    前端显示「新增 10 篇」——用户以为抓取有问题，其实一切正常。
    """

    def test_crawl_result_has_new_posts_field(self):
        from app.parser.base import CrawlResult

        r = CrawlResult(url="https://x.com", status="success")
        assert r.new_posts == 0
        assert r.post_count == 0

    def test_admin_uses_new_posts_not_post_count(self):
        """单站点抓取的状态里，new_posts 必须用 new_posts。"""
        import inspect

        from app.api.admin import _run_crawl

        src = inspect.getsource(_run_crawl)
        assert '"new_posts": result.new_posts' in src, (
            "应该用 result.new_posts（真正新增），不是 result.post_count（本次抓到）"
        )
        assert '"new_posts": result.post_count' not in src

    @pytest.mark.asyncio
    async def test_recrawl_reports_zero_new(self, monkeypatch):
        """重复抓取同一篇文章，应该报告新增 0 篇。"""
        from app.models import Base, Post, Site, User
        from app.parser.base import CrawlResult, ParsedPost, ParsedSite
        from app.services.crawler import save_crawl_result
        from sqlalchemy import select
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
            site = Site(user_id=user.id, name="s", url="https://a.com", url_hash="h")
            db.add(site)
            await db.flush()

            result = CrawlResult(
                url="https://a.com",
                status="success",
                strategy="feed",
                post_count=1,
                site=ParsedSite(
                    url="https://a.com",
                    name="s",
                    strategy="feed",
                    posts=[
                        ParsedPost(
                            title="一",
                            link="https://a.com/post/1/",
                            strategy="feed",
                            guid="wp:1",
                        )
                    ],
                ),
            )

            n1 = await save_crawl_result(db, site, result)
            await db.commit()
            n2 = await save_crawl_result(db, site, result)
            await db.commit()

            assert n1 == 1
            assert n2 == 0, f"重复抓取应新增 0 篇，实际 {n2}"

            rows = (
                await db.execute(select(Post).where(Post.site_id == site.id))
            ).scalars().all()
            assert len(rows) == 1

        await engine.dispose()


class TestModelField:
    def test_content_hash_column_exists(self):
        from app.models.post import Post

        assert "content_hash" in Post.__table__.columns
        assert Post.__table__.columns["content_hash"].nullable

    def test_guid_hash_algorithm_untouched(self):
        """guid_hash 不带 guid 参数——历史数据靠它。"""
        import inspect

        from app.core.security import guid_hash

        sig = inspect.signature(guid_hash)
        assert list(sig.parameters) == ["url"]
