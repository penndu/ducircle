"""抓取失败的几个根因的回归测试。

这些 bug 都有同一个特点：**单测看不出来，只有真站点才会暴露**。
所以这里用构造的 HTML/URL 把行为钉死。

1. www 被剥离 → 裸域没有 DNS → ConnectError
2. /archives/xxx/ 是 Typecho 的文章永久链接，不是归档页
3. /2026 可能是文章 ID（递增数字做永久链接）
4. 站点换域名（vjo.cc → liuz.cn）后，相对路径要按最终地址拼
"""

import pytest

from app.core.security import normalize_url, request_url


class TestRequestUrlKeepsWww:
    def test_request_url_keeps_www(self):
        """发请求的地址必须保留 www.。"""
        assert request_url("https://www.iczrx.cn") == "https://www.iczrx.cn/"
        assert request_url("https://www.iczrx.cn/feed/") == (
            "https://www.iczrx.cn/feed"
        )

    def test_normalize_url_still_strips_www(self):
        """去重键照旧剥 www.——同一个博客只能加一次。"""
        assert normalize_url("https://www.iczrx.cn") == "https://iczrx.cn/"

    def test_two_functions_differ_only_on_www(self):
        """两者只在 www 上有区别，别的地方行为要一致。"""
        for url in [
            "https://example.com/a/",
            "https://example.com/a?b=1",
            "https://example.com:443/x",
        ]:
            assert normalize_url(url) == request_url(url)

    def test_request_url_keeps_query(self):
        """请求地址要保留查询参数，追踪参数剥离只用于去重。"""
        assert "utm_source" in request_url("https://example.com/p?utm_source=x")

    def test_request_url_drops_fragment(self):
        assert "#" not in request_url("https://example.com/p#top")


class TestArticlePathFiltering:
    def test_typecho_permalink_is_article(self):
        """/archives/158/ 是 Typecho 的永久链接，是文章不是归档页。"""
        from app.parser.heuristic import looks_like_article

        assert looks_like_article("https://example.com/archives/158/") is True
        assert looks_like_article("https://example.com/archives/frp.html") is True

    def test_real_archive_pages_still_filtered(self):
        """真正的归档列表页仍然要过滤掉。"""
        from app.parser.heuristic import looks_like_article

        assert looks_like_article("https://example.com/archives/") is False
        assert looks_like_article("https://example.com/archives/2026") is False
        assert looks_like_article("https://example.com/archives/2026/") is False
        assert looks_like_article("https://example.com/2026/07/") is False

    def test_numeric_id_articles_kept(self):
        """/2027 这类 4 位数字可能是文章 ID，不能当成归档页。"""
        from app.parser.heuristic import looks_like_article

        assert looks_like_article("https://example.com/2027") is True
        assert looks_like_article("https://example.com/2020") is True

    def test_small_numbers_still_pagination(self):
        """1~3 位数字仍然是分页。"""
        from app.parser.heuristic import looks_like_article

        assert looks_like_article("https://example.com/2") is False
        assert looks_like_article("https://example.com/2/") is False


class TestFeedDiscovery:
    def test_common_feed_paths_cover_real_world(self):
        """常见 feed 路径要覆盖 WordPress / Typecho / Hugo 等。"""
        from app.parser.feed import COMMON_FEED_PATHS

        assert "/feed" in COMMON_FEED_PATHS
        assert "/feed/" in COMMON_FEED_PATHS
        assert "/rss.xml" in COMMON_FEED_PATHS
        assert "/atom.xml" in COMMON_FEED_PATHS
        assert "/index.xml" in COMMON_FEED_PATHS

    def test_declared_feed_found(self):
        """页面里声明的 <link rel="alternate"> 要能发现。"""
        from app.parser.feed import discover_feed_url

        html = """
        <html><head>
        <link rel="alternate" type="application/rss+xml"
              title="RSS 2.0" href="https://example.com/feed/" />
        </head><body></body></html>
        """
        assert discover_feed_url(html, "https://example.com") == (
            "https://example.com/feed/"
        )


class TestHeuristicContainers:
    def test_card_box_container(self):
        """国内博客常用的 card-box 结构要能识别。"""
        from app.parser.heuristic import parse_with_heuristic

        html = """
        <div class="col-lg-12">
          <div class="card-box mb-2"><div class="media">
            <div class="media-body"><h4><a href="/blog/article/1.html">文章一</a></h4>
            </div></div></div>
          <div class="card-box mb-2"><div class="media">
            <div class="media-body"><h4><a href="/blog/article/2.html">文章二</a></h4>
            </div></div></div>
          <div class="card-box mb-2"><div class="media">
            <div class="media-body"><h4><a href="/blog/article/3.html">文章三</a></h4>
            </div></div></div>
        </div>
        """
        site = parse_with_heuristic(html, "https://example.com")
        assert site is not None, "card-box 结构应能解析"
        assert len(site.posts) == 3
        assert site.strategy == "heuristic"

    def test_parsed_links_keep_www(self):
        """解析出的文章链接要保留 www——后面还要去抓正文。"""
        from app.parser.heuristic import parse_with_heuristic

        html = """
        <article><h2><a href="/a/">文章一</a></h2></article>
        <article><h2><a href="/b/">文章二</a></h2></article>
        <article><h2><a href="/c/">文章三</a></h2></article>
        """
        site = parse_with_heuristic(html, "https://www.example.com")
        assert site is not None
        for p in site.posts:
            assert p.link.startswith("https://www.example.com/"), (
                f"文章链接丢了 www：{p.link}"
            )


class TestFeedStrategyOnlyTriesOnce:
    @pytest.mark.asyncio
    async def test_try_feed_called_once(self, monkeypatch):
        """_apply_strategies 里 _try_feed 只能调一次。

        之前的写法是 `if await self._try_feed(...) is not None:
        return await self._try_feed(...)`，等于把整个 RSS 探测跑两遍，
        每个候选路径都发两次请求——10 个站点就是几十次多余请求。
        """
        from app.services.crawler import Crawler

        calls = {"n": 0}

        async def _fake_try_feed(self, url, html):
            calls["n"] += 1
            return None

        monkeypatch.setattr(Crawler, "_try_feed", _fake_try_feed)
        async with Crawler() as crawler:
            await crawler._apply_strategies("https://example.com", "<html></html>")
        assert calls["n"] == 1, f"_try_feed 被调用了 {calls['n']} 次，应该只调 1 次"


class TestDuplicateGuidHandling:
    """guid_hash 重复不该让整个站点的抓取结果全丢。

    生产库的 posts 表缺唯一约束，历史数据里存在重复行。
    落库时用 scalar_one_or_none() 查重复文章，撞到多行直接抛
    MultipleResultsFound——表现就是「某个站点永远抓失败」。
    """

    @staticmethod
    def _code_lines(func) -> str:
        """只取代码行，去掉注释和 docstring。

        注释里会提到 scalar_one_or_none 这个词，不能算作实际使用。
        """
        import inspect

        src = inspect.getsource(func)
        lines = []
        for line in src.splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            lines.append(line)
        return "\n".join(lines)

    @pytest.mark.asyncio
    async def test_existing_post_lookup_tolerates_duplicates(self):
        """查重复文章时用 first()，多行也只取一条。"""
        from app.services.crawler import save_crawl_result

        code = self._code_lines(save_crawl_result)
        assert "scalar_one_or_none()" not in code, (
            "查重复文章仍用 scalar_one_or_none()，遇到重复行会抛异常"
        )
        assert ".first()" in code

    @pytest.mark.asyncio
    async def test_resolve_site_id_tolerates_duplicates(self):
        from app.services.crawler import _resolve_site_id

        code = self._code_lines(_resolve_site_id)
        assert "scalar_one_or_none()" not in code
        assert ".first()" in code

    def test_migration_exists(self):
        """补唯一约束的迁移必须在，否则新库同样会缺。"""
        from pathlib import Path

        migrations = Path("alembic/versions")
        names = {p.name for p in migrations.glob("*.py")}
        assert any("guid_unique" in n for n in names), (
            "缺少补 posts 唯一约束的迁移"
        )

    def test_migration_cleans_duplicates_before_creating_constraint(self):
        """迁移必须先清重复再建约束，否则 MySQL 会拒绝。"""
        from pathlib import Path

        content = (
            Path("alembic/versions/0004_post_guid_unique.py").read_text(
                encoding="utf-8"
            )
        )
        assert "deleted_at = NOW()" in content
        assert "create_unique_constraint" in content
        # 清理语句必须排在建约束之前
        assert content.index("deleted_at = NOW()") < content.index(
            "create_unique_constraint"
        )
