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


class TestSummaryFailedMarker:
    """永久性失败的文章不该被反复重试。"""

    def test_model_has_failed_fields(self):
        from app.models.post import Post

        cols = Post.__table__.columns
        assert "summary_failed" in cols
        assert "summary_error" in cols

    def test_batch_query_skips_failed(self):
        """批量生成要跳过永久失败的文章。"""
        import inspect

        from app.services.ai_summary import summarize_pending_for_user

        src = inspect.getsource(summarize_pending_for_user)
        assert "Post.summary_failed.is_(False)" in src, (
            "批量生成要跳过 summary_failed 的文章"
        )

    def test_reset_clears_failed_flag(self):
        """手动点「重新生成」要清掉失败标记。"""
        import inspect

        from app.api.ai import reset_summary

        src = inspect.getsource(reset_summary)
        assert "summary_failed = False" in src
        assert "summary_error = None" in src

    @pytest.mark.asyncio
    async def test_permanent_failure_marked(self, monkeypatch):
        """不可重试的 LLM 错误要标记文章。"""
        from app.models import Base, Post, Site, User
        from app.parser.base import ParsedPost, ParsedSite
        from app.services.ai_summary import summarize_post
        from app.services.llm import LLMError
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
            user = User(username="u1", password_hash="x", ai_api_key="k")
            db.add(user)
            await db.flush()
            site = Site(user_id=user.id, name="s", url="https://x.com", url_hash="h")
            db.add(site)
            await db.flush()
            post = Post(
                user_id=user.id, site_id=site.id, title="一",
                link="https://x.com/p/1/", guid_hash="g1",
                raw_content="<p>" + "正文内容。" * 100 + "</p>",
            )
            db.add(post)
            await db.flush()

            class _FailClient:
                model = "test"

                async def complete(self, *a, **kw):
                    raise LLMError("请求被拒 422", retryable=False)

            import app.services.ai_summary as mod

            orig = mod._build_client
            mod._build_client = lambda u, s: (_FailClient(), "test")
            try:
                await summarize_post(post, user, db)
            finally:
                mod._build_client = orig

            assert post.summary_failed is True, "不可重试的失败应标记"
            assert post.summary_error
            assert post.summary is None

        await engine.dispose()


class TestSalvageSummary:
    """模型返回不合法 JSON 时，不能把原文当摘要存进库。

    实测踩过：模型返回的 JSON 里摘要含未转义引号，
    json.loads 失败，fallback 把半截 `{"summary":"作者...` 存进了数据库，
    页面上直接显示 JSON 原文——比没摘要更糟。
    """

    def test_valid_json_unchanged(self):
        from app.services.llm import parse_summary_response

        s, score, reason = parse_summary_response(
            '{"summary": "作者讲了一件事", "score": 7.5, "reason": "有价值"}'
        )
        assert s == "作者讲了一件事"
        assert score == 7.5
        assert reason == "有价值"

    def test_broken_json_salvaged(self):
        """JSON 不合法时也要能抠出 summary。"""
        from app.services.llm import parse_summary_response

        broken = '{"summary": "作者说"这是关键", "score": 8, "reason": "有洞见"}'
        s, score, reason = parse_summary_response(broken)
        assert s is not None
        assert not s.lstrip().startswith('{"summary"'), "不能暴露 JSON 原文"
        assert "作者说" in s
        assert score == 8.0

    def test_plain_text_fallback(self):
        """纯文本摘要照常当摘要用。"""
        from app.services.llm import parse_summary_response

        s, score, reason = parse_summary_response("这是一段纯文本摘要，不带 JSON。")
        assert s == "这是一段纯文本摘要，不带 JSON。"
        assert score is None

    def test_empty_summary_not_salvaged(self):
        from app.services.llm import parse_summary_response

        s, score, reason = parse_summary_response(
            '{"summary": "", "score": 5}'
        )
        assert s == ""
        assert score is None

    def test_no_summary_key_returns_empty(self):
        """合法 JSON 但没有 summary 字段 → 视为没生成出来。

        不能当纯文本存——存进去用户看到的就是 `{"foo": "bar"}`。
        """
        from app.services.llm import parse_summary_response

        s, score, reason = parse_summary_response('{"foo": "bar"}')
        assert s == ""
        assert score is None

    def test_score_clamped(self):
        from app.services.llm import parse_summary_response

        s, score, reason = parse_summary_response(
            '{"summary": "x", "score": 99}'
        )
        assert score == 10.0

    def test_truncated_json_keeps_tail(self):
        """JSON 被截断时，内容要保到末尾。

        实测踩过：max_tokens 不够，模型没写完就停了，
        输出是 `{"summary":"作者把 Multica 用作 Agent 看板：...配置 Code`。
        """
        from app.services.llm import parse_summary_response

        truncated = (
            '{"summary":"作者把 Multica 用作 Agent 看板：任务以 issue 形式流转，'
            '本机 daemon 作为 runtime 接入，可为不同 Agent 配置 Code'
        )
        s, score, reason = parse_summary_response(truncated)
        assert not s.lstrip().startswith('{"summary"')
        assert s.endswith("配置 Code"), f"截断的尾巴丢了：{s[-30:]!r}"

    def test_salvage_keeps_score_when_json_broken(self):
        from app.services.llm import parse_summary_response

        broken = '{"summary": "作者说"这是关键", "score": 8, "reason": "有洞见"}'
        s, score, reason = parse_summary_response(broken)
        assert score == 8.0
        assert reason == "有洞见"
