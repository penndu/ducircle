"""用 RSS feed 里的全文补上「SPA 站抓不到正文」的问题。

实测：obaby@mars（zhongxiaojie.com）的文章页只返回 1700 字节的
SPA 骨架页，正文全靠 JS 渲染——静态抓取拿不到，
摘要生成 90% 失败在「正文抽取失败」上。

但这些站的 feed 里带着全文（WordPress 默认输出 content:encoded）。
抓取时存下来，摘要生成直接用。
"""

import pytest


class TestFeedContentExtraction:
    def test_content_encoded_extracted(self):
        """feed 里的 content:encoded 要能取出来。"""
        from app.parser.feed import _extract_content

        entry = {
            "content": [
                {"value": "<p>" + "正文内容。" * 100 + "</p>"},
            ]
        }
        out = _extract_content(entry)
        assert out is not None
        assert "正文内容" in out

    def test_short_content_ignored(self):
        """太短的不是全文（可能只是摘要），不要。"""
        from app.parser.feed import _extract_content

        entry = {"content": [{"value": "<p>太短了</p>"}]}
        assert _extract_content(entry) is None

    def test_no_content_returns_none(self):
        from app.parser.feed import _extract_content

        assert _extract_content({}) is None
        assert _extract_content({"content": []}) is None
        assert _extract_content({"content": [{"value": ""}]}) is None

    def test_parsed_post_has_content_field(self):
        from app.parser.base import ParsedPost

        p = ParsedPost(title="一", link="https://x.com/1/")
        assert p.content is None

    def test_feed_parser_carries_content(self):
        """解析整篇 feed 时要把全文带上。"""
        from app.parser.feed import parse_feed

        xml = """<?xml version="1.0"?>
        <rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/">
          <channel>
            <title>站点</title>
            <item>
              <title>文章一</title>
              <link>https://x.com/post/1/</link>
              <guid isPermaLink="false">https://x.com/?p=1</guid>
              <content:encoded><![CDATA[<p>""" + "这是正文内容。" * 50 + """</p>]]></content:encoded>
            </item>
          </channel>
        </rss>"""

        site = parse_feed(xml.encode("utf-8"), "https://x.com/feed/", "https://x.com")
        assert site is not None
        post = site.posts[0]
        assert post.content is not None
        assert "这是正文内容" in post.content
        assert post.guid == "wp:1"


class TestExtractContentFallback:
    @pytest.mark.asyncio
    async def test_prefers_stored_feed_content(self):
        """有存下来的 feed 全文时，不该再去抓页面。"""
        from app.services.ai_summary import _extract_content

        class _FakePost:
            link = "https://x.com/post/1/"
            raw_content = "<p>" + "正文" * 200 + "</p>"

        called = []

        async def _fake_fetch(link, site_url):
            called.append(link)
            return "不该走到这里"

        import app.services.ai_summary as mod

        orig = mod._fetch_and_extract
        mod._fetch_and_extract = _fake_fetch
        try:
            text = await _extract_content(_FakePost(), "https://x.com")
        finally:
            mod._fetch_and_extract = orig

        assert text is not None
        assert not called, "有 feed 全文就不该再抓页面"

    @pytest.mark.asyncio
    async def test_falls_back_to_page_when_no_feed_content(self):
        """没存全文时才去抓页面。"""
        from app.services.ai_summary import _extract_content

        class _FakePost:
            link = "https://x.com/post/1/"
            raw_content = None

        called = []

        async def _fake_fetch(link, site_url):
            called.append(link)
            return "页面正文"

        import app.services.ai_summary as mod

        orig = mod._fetch_and_extract
        mod._fetch_and_extract = _fake_fetch
        try:
            text = await _extract_content(_FakePost(), "https://x.com")
        finally:
            mod._fetch_and_extract = orig

        assert text == "页面正文"
        assert called == ["https://x.com/post/1/"]


class TestRawContentStorage:
    def test_post_model_has_raw_content(self):
        from app.models.post import Post

        assert "raw_content" in Post.__table__.columns

    def test_crawler_stores_feed_content(self):
        """落库时要存 feed 全文。"""
        import inspect

        from app.services.crawler import save_crawl_result

        src = inspect.getsource(save_crawl_result)
        assert "raw_content=parsed.content" in src, (
            "落库时要把 feed 全文存进 raw_content"
        )

    def test_backfill_script_exists(self):
        from pathlib import Path

        path = Path("scripts/backfill_raw_content.py")
        assert path.exists()
        content = path.read_text(encoding="utf-8")
        assert "raw_content" in content
