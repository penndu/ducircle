"""AI 摘要与导出测试。

重点验证三件事：
1. 额度控制不能被绕过（这是花钱的地方）
2. 内容哈希缓存真的能避免重复调用
3. 导出格式正确且完整
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


def make_user(user_id: int, **kwargs) -> "User":
    from app.models import User

    return User(
        id=user_id,
        username=f"u{user_id}",
        password_hash="x",
        **kwargs,
    )


class TestQuota:
    """额度控制。这是唯一直接花钱的地方，必须严格。"""

    @pytest.mark.asyncio
    async def test_platform_quota_counts(self, db):
        from datetime import date

        from app.core.tenant import set_tenant
        from app.models import AIQuotaLog, Post, Site
        from app.services.ai_summary import check_quota

        set_tenant(1)
        user = make_user(1, ai_enabled=True)
        db.add(user)
        db.add(Site(id=1, user_id=1, name="s", url="https://s.com",
                    url_hash="h1", source="manual"))
        db.add(Post(id=1, user_id=1, site_id=1, title="t",
                    link="https://s.com/1", guid_hash="g1"))
        await db.commit()

        allowed, used, limit = await check_quota(user, db)
        assert allowed is True and used == 0

        # 加一条额度记录
        db.add(AIQuotaLog(
            user_id=1, post_id=1, quota_date=date.today(),
            provider="siliconflow", from_user_key=False,
        ))
        await db.commit()
        allowed, used, _ = await check_quota(user, db)
        assert used == 1

    @pytest.mark.asyncio
    async def test_quota_exhausted_blocks(self, db):
        from datetime import date

        from app.core.tenant import set_tenant
        from app.models import AIQuotaLog, Post, Site
        from app.services.ai_summary import check_quota

        set_tenant(1)
        user = make_user(1, ai_enabled=True, ai_daily_quota_override=2)
        db.add(user)
        db.add(Site(id=1, user_id=1, name="s", url="https://s.com",
                    url_hash="h1", source="manual"))
        db.add_all([
            Post(id=i, user_id=1, site_id=1, title=f"t{i}",
                 link=f"https://s.com/{i}", guid_hash=f"g{i}")
            for i in range(1, 3)
        ])
        await db.commit()

        # 用满 2 篇
        db.add_all([
            AIQuotaLog(user_id=1, post_id=1, quota_date=date.today(),
                       provider="p", from_user_key=False),
            AIQuotaLog(user_id=1, post_id=2, quota_date=date.today(),
                       provider="p", from_user_key=False),
        ])
        await db.commit()

        allowed, used, limit = await check_quota(user, db)
        assert allowed is False, "额度用完必须拒绝"
        assert used == 2 and limit == 2
        set_tenant(None)

    @pytest.mark.asyncio
    async def test_own_key_bypasses_quota(self, db):
        """用户自带 Key 时不受额度限制——这是 Q19-C 的核心承诺。"""
        from app.core.tenant import set_tenant
        from app.services.ai_summary import check_quota

        set_tenant(1)
        user = make_user(
            1, ai_enabled=True, ai_api_key="sk-user-key",
            ai_daily_quota_override=1,
        )
        allowed, used, _ = await check_quota(user, db)
        assert allowed is True, "自带 Key 时即使额度上限是1 也应放行"
        set_tenant(None)

    @pytest.mark.asyncio
    async def test_disabled_ai_blocks(self, db):
        from app.core.tenant import set_tenant
        from app.services.ai_summary import check_quota

        set_tenant(1)
        user = make_user(1, ai_enabled=False, ai_api_key="sk-x")
        allowed, _, _ = await check_quota(user, db)
        assert allowed is False, "用户关闭 AI 后必须拒绝"
        set_tenant(None)

    @pytest.mark.asyncio
    async def test_user_quota_not_counted(self, db):
        """自带 Key 的调用不计入平台额度——否则会误伤免费额度。"""
        from datetime import date

        from app.core.tenant import set_tenant
        from app.models import AIQuotaLog, Post, Site
        from app.services.ai_summary import check_quota

        set_tenant(1)
        user = make_user(1, ai_enabled=True, ai_daily_quota_override=3)
        db.add(user)
        db.add(Site(id=1, user_id=1, name="s", url="https://s.com",
                    url_hash="h1", source="manual"))
        db.add_all([
            Post(id=i, user_id=1, site_id=1, title=f"t{i}",
                 link=f"https://s.com/{i}", guid_hash=f"g{i}")
            for i in range(1, 4)
        ])
        await db.commit()

        # 注意 (user_id, quota_date, post_id) 上有唯一约束，
        # 同一篇文章同一天只能有一条记录。额度消耗靠不同 post_id 体现。
        db.add_all([
            AIQuotaLog(user_id=1, post_id=1, quota_date=date.today(),
                       provider="gemini", from_user_key=True),
            AIQuotaLog(user_id=1, post_id=2, quota_date=date.today(),
                       provider="gemini", from_user_key=True),
            AIQuotaLog(user_id=1, post_id=3, quota_date=date.today(),
                       provider="gemini", from_user_key=True),
        ])
        await db.commit()

        allowed, used, _ = await check_quota(user, db)
        assert allowed is True, "自带 Key 的消耗不应计入平台额度"
        assert used == 0
        set_tenant(None)


class TestContentCache:
    """内容哈希缓存——避免重复花钱的最有效手段。"""

    def test_needs_summary_when_empty(self):
        from app.services.extractor import needs_summary

        assert needs_summary(None, "hash1") is True

    def test_skips_when_unchanged(self):
        from app.services.extractor import needs_summary

        assert needs_summary("hash1", "hash1") is False, "正文没变不该重新生成"

    def test_regenerates_when_changed(self):
        from app.services.extractor import needs_summary

        assert needs_summary("hash1", "hash2") is True, "正文变了要重新生成"

    def test_hash_stable_and_distinct(self):
        from app.services.extractor import content_hash

        a = content_hash("同样的内容")
        b = content_hash("同样的内容")
        c = content_hash("不同的内容")
        assert a == b
        assert a != c


class TestSummaryParsing:
    """模型返回的 JSON 必须能防御性解析。"""

    def test_plain_json(self):
        from app.services.llm import parse_summary_response

        summary, score, reason = parse_summary_response(
            '{"summary": "讲了个技术故事", "score": 8, "reason": "有干货"}'
        )
        assert summary == "讲了个技术故事"
        assert score == 8.0
        assert reason == "有干货"

    def test_markdown_wrapped(self):
        """模型经常把 JSON 包在 ```json 里。"""
        from app.services.llm import parse_summary_response

        text = '```json\n{"summary": "摘要", "score": 7}\n```'
        summary, score, _ = parse_summary_response(text)
        assert summary == "摘要"
        assert score == 7.0

    def test_score_clamped(self):
        """模型给出离谱分数时要夹紧，不能直接存进去。"""
        from app.services.llm import parse_summary_response

        _, high, _ = parse_summary_response('{"summary":"x","score":999}')
        assert high == 10.0

        _, low, _ = parse_summary_response('{"summary":"x","score":-5}')
        assert low == 0.0

    def test_invalid_json_degrades_to_text(self):
        """模型没按要求返回 JSON 时，把文本当摘要用，而不是丢弃。"""
        from app.services.llm import parse_summary_response

        summary, score, _ = parse_summary_response("这是一段普通文本摘要。")
        assert summary == "这是一段普通文本摘要。"
        assert score is None

    def test_empty_summary_returns_empty(self):
        """摘要为空时不抛异常，返回空串让调用方决定跳过。"""
        from app.services.llm import parse_summary_response

        summary, score, _ = parse_summary_response('{"summary": ""}')
        assert summary == ""
        assert score is None


class TestExtractor:
    def test_extracts_article_body(self):
        from app.services.extractor import extract_content

        html = """
        <html><head><title>测试</title></head><body>
        <nav><a href="/a">导航一</a><a href="/b">导航二</a></nav>
        <article class="post-content">
          <p>""" + "这是一篇足够长的正文内容，用来验证抽取是否有效。" * 10 + """</p>
          <p>""" + "更多正文内容，确保长度超过阈值。" * 10 + """</p>
        </article>
        <footer>页脚版权信息</footer>
        </body></html>
        """
        result = extract_content(html)
        assert result is not None
        assert len(result) > 100

    def test_rejects_list_page(self):
        """站点聚合列表不该被当成正文。

        实测踩过：传了站点首页 URL，抽出来的是「站点首页的文章列表」，
        看着有内容实则是导航条目——喂给模型只会生成废话摘要。
        样本取自noionion.top 首页的真实抽取结果。
        """
        from app.services.extractor import _looks_like_article

        # 真实列表页的形态：每条一行、都很短
        list_text = "\n".join([
            "每日一题",
            "秋招经验分享",
            "个人哔哔：谈薪时间",
            "从零开始的主题开发",
            "某某某的另一篇",
            "还有更多",
            "最后一篇",
            "更多更多",
            "又一篇",
            "再一篇",
        ])
        assert _looks_like_article(list_text) is False, "列表页必须被识别出来"

    def test_accepts_article_with_some_short_lines(self):
        """正文里也会有短句，不能因为「有短行」就误判成列表。"""
        from app.services.extractor import _looks_like_article

        article = "\n".join([
            "好久不见。",
            "昨天跟 AI 聊了两个小时，聊出十七个好想法，然后一个都没做。",
            "最近一周高强度做思考性工作，同时大量用 AI。一个感受越来越清晰："
            "有了 AI 之后，人其实不缺思考，缺的是结论和行动。",
            "所以今天想聊聊这个问题。它困扰我很久了，也困扰很多人。",
            "于是我决定做一件最小的事，先做十分钟，再决定要不要继续。",
        ])
        assert _looks_like_article(article) is True

    def test_accepts_single_paragraph_article(self):
        """单行长文是合法的，不能因为「行数少」被误杀。"""
        from app.services.extractor import _looks_like_article

        single_line = "这是一段没有任何换行的长正文，" * 15
        assert _looks_like_article(single_line) is True

    def test_rejects_too_short(self):
        from app.services.extractor import _looks_like_article

        assert _looks_like_article("太短了") is False

    def test_returns_none_for_tiny_html(self):
        from app.services.extractor import extract_content

        assert extract_content("<html></html>") is None

    def test_normalize_collapses_whitespace(self):
        from app.services.extractor import _normalize

        raw = "第一段   有很多   空格\n\n\n\n第二段"
        result = _normalize(raw, max_chars=0)
        assert "   " not in result
        assert "\n\n\n" not in result

    def test_truncate_at_sentence_boundary(self):
        from app.services.extractor import _normalize

        text = "第一句话。" * 100
        result = _normalize(text, max_chars=50)
        assert len(result) <= 80
        assert "截断" in result


class TestExport:
    @pytest.mark.asyncio
    async def test_export_json_structure(self, db):
        from app.core.security import site_hash
        from app.core.tenant import set_tenant
        from app.models import Post, Site
        from app.services.exporter import export_all, to_json

        set_tenant(1)
        db.add(Site(id=1, user_id=1, name="站点A", url="https://a.com",
                    url_hash=site_hash("https://a.com"), source="manual"))
        db.add(Post(id=1, user_id=1, site_id=1, title="文章一",
                    link="https://a.com/1", guid_hash="g1", summary="摘要一"))
        await db.commit()

        data = await export_all(db)
        assert data["version"] == 1
        assert len(data["sites"]) == 1
        assert len(data["posts"]) == 1

        import json

        parsed = json.loads(to_json(data))
        assert parsed["sites"][0]["name"] == "站点A"
        assert parsed["posts"][0]["summary"] == "摘要一"
        set_tenant(None)

    @pytest.mark.asyncio
    async def test_export_csv_has_bom(self, db):
        """Excel 打开 CSV 需要 BOM 才能正确识别 UTF-8 中文。"""
        from app.core.security import site_hash
        from app.core.tenant import set_tenant
        from app.models import Site
        from app.services.exporter import export_all, to_csv

        set_tenant(1)
        db.add(Site(id=1, user_id=1, name="中文站名", url="https://a.com",
                    url_hash=site_hash("https://a.com"), source="manual"))
        await db.commit()

        csv_text = to_csv(await export_all(db))
        assert csv_text.startswith("﻿"), "CSV 必须以 BOM 开头"
        assert "中文站名" in csv_text
        set_tenant(None)

    @pytest.mark.asyncio
    async def test_export_markdown(self, db):
        from app.core.security import site_hash
        from app.core.tenant import set_tenant
        from app.models import Post, Site
        from app.services.exporter import export_all, to_markdown

        set_tenant(1)
        db.add(Site(id=1, user_id=1, name="站点A", url="https://a.com",
                    url_hash=site_hash("https://a.com"), source="manual"))
        db.add(Post(id=1, user_id=1, site_id=1, title="文章标题",
                    link="https://a.com/1", guid_hash="g1", summary="文章摘要"))
        await db.commit()

        md = to_markdown(await export_all(db))
        assert "# 友链朋友圈导出" in md
        assert "文章标题" in md
        assert "文章摘要" in md
        assert "https://a.com" in md
        set_tenant(None)

    @pytest.mark.asyncio
    async def test_export_isolated_between_users(self, db):
        """导出绝不能带上别人的数据。"""
        from sqlalchemy import select

        from app.core.security import site_hash
        from app.core.tenant import as_tenant_async, set_tenant
        from app.models import Site
        from app.services.exporter import export_all

        set_tenant(1)
        db.add(Site(id=1, user_id=1, name="Alice的站点",
                    url="https://alice.com",
                    url_hash=site_hash("https://alice.com"), source="manual"))
        await db.commit()

        alice_data = await export_all(db)
        assert len(alice_data["sites"]) == 1

        async with as_tenant_async(2):
            bob_data = await export_all(db)
            assert len(bob_data["sites"]) == 0, "Bob 导出不该看到 Alice 的数据"
        set_tenant(None)


import json  # noqa: E402