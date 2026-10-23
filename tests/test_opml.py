"""OPML 解析与导入测试。

用真实导出器的格式做样本。FreshRSS / TinyRSS / Feedly 的字段组合各不相同，
这是实测中最容易出错的地方。
"""

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool


# ---- 真实导出器格式样本 ----

FRESH_RSS_NESTED = """<?xml version="1.0" encoding="UTF-8"?>
<opml version="2.0">
  <head>
    <title>FreshRSS 订阅</title>
    <dateCreated>Mon, 01 Jan 2024 00:00:00 +0800</dateCreated>
  </head>
  <body>
    <outline text="技术">
      <outline type="rss" text="Akilar的糖果屋"
               xmlUrl="https://akilar.top/atom.xml"
               htmlUrl="https://akilar.top/"/>
      <outline type="rss" text="Elizen"
               xmlUrl="https://elizen.me/index.xml"
               htmlUrl="https://elizen.me/"/>
    </outline>
    <outline text="生活">
      <outline type="rss" text="某个博客"
               xmlUrl="https://blog.example.com/feed"
               htmlUrl="https://blog.example.com/"/>
    </outline>
  </body>
</opml>"""

TINYRSS_FLAT = """<?xml version="1.0" encoding="UTF-8"?>
<opml version="1.0">
  <head><title>我的订阅</title></head>
  <body>
    <outline type="rss" title="标题用title属性"
             htmlUrl="https://t1.example.com/"
             xmlUrl="https://t1.example.com/rss.xml"/>
    <outline type="rss" text="标题用text属性"
             htmlUrl="https://t2.example.com/"
             xmlUrl="https://t2.example.com/rss.xml"/>
  </body>
</opml>"""

# 只有 feed 地址，没有 htmlUrl —— 很多导出器是这样
FEED_ONLY = """<?xml version="1.0" encoding="UTF-8"?>
<opml version="2.0">
  <head><title>只有feed</title></head>
  <body>
    <outline type="rss" text="站点A" xmlUrl="https://only-feed-a.com/atom.xml"/>
    <outline type="rss" text="站点B" xmlUrl="https://only-feed-b.com/feed.xml"/>
  </body>
</opml>"""

# 同一站点多个 feed —— 用户常订阅全文+分类
DUPLICATES = """<?xml version="1.0" encoding="UTF-8"?>
<opml version="2.0">
  <head><title>重复订阅</title></head>
  <body>
    <outline type="rss" text="站点X" htmlUrl="https://dup.com/" xmlUrl="https://dup.com/full.xml"/>
    <outline type="rss" text="站点X分类" htmlUrl="https://dup.com/" xmlUrl="https://dup.com/cat.xml"/>
  </body>
</opml>"""

# 带 HTML 标记的 text
HTML_IN_TEXT = """<?xml version="1.0" encoding="UTF-8"?>
<opml version="2.0">
  <head><title>带HTML</title></head>
  <body>
    <outline type="rss" text="&lt;b&gt;加粗标题&lt;/b&gt;&amp;更多"
             htmlUrl="https://html.example.com/" xmlUrl="https://html.example.com/f.xml"/>
  </body>
</opml>"""

OPML_1_0 = """<?xml version="1.0" encoding="UTF-8"?>
<opml version="1.0">
  <head><title>老格式</title></head>
  <body>
    <outline title="老站点" type="rss"
             htmlUrl="https://old.example.com/" xmlUrl="https://old.example.com/rss"/>
  </body>
</opml>"""


class TestParsing:
    def test_nested_categories(self):
        from app.services.opml_parser import parse_opml

        result = parse_opml(FRESH_RSS_NESTED)
        assert result.title == "FreshRSS 订阅"
        assert len(result.entries) == 3

        names = {e.name for e in result.entries}
        assert names == {"Akilar的糖果屋", "Elizen", "某个博客"}

        # 分类要正确继承
        by_name = {e.name: e for e in result.entries}
        assert by_name["Akilar的糖果屋"].category == "技术"
        assert by_name["某个博客"].category == "生活"

    def test_htmlurl_is_site_not_feed(self):
        """最关键的正确性：htmlUrl 是站点，xmlUrl 是 feed。

        拿 xmlUrl 当站点 URL 会导致抓取必然失败——抓到的是 XML 不是 HTML。
        """
        from app.services.opml_parser import parse_opml

        result = parse_opml(FRESH_RSS_NESTED)
        entry = next(e for e in result.entries if e.name == "Akilar的糖果屋")

        assert entry.site_url == "https://akilar.top/"
        assert entry.feed_url == "https://akilar.top/atom.xml"
        assert entry.inferred_site is False

    def test_title_or_text_attribute(self):
        """不同导出器用 title 或 text，放哪个都要认。"""
        from app.services.opml_parser import parse_opml

        result = parse_opml(TINYRSS_FLAT)
        names = {e.name for e in result.entries}
        assert "标题用title属性" in names
        assert "标题用text属性" in names

    def test_feed_only_infers_site(self):
        """只有 feed 地址时推断站点根目录，并标记待确认。"""
        from app.services.opml_parser import parse_opml

        result = parse_opml(FEED_ONLY)
        assert len(result.entries) == 2
        for entry in result.entries:
            assert entry.inferred_site is True
            assert entry.feed_url is not None

        by_name = {e.name: e for e in result.entries}
        assert by_name["站点A"].site_url == "https://only-feed-a.com/"

    def test_duplicates_merged(self):
        """同一站点多个 feed 应该合并成一条，且保留 feed 地址。"""
        from app.services.opml_parser import dedupe_entries, parse_opml

        result = parse_opml(DUPLICATES)
        merged, dropped = dedupe_entries(result.entries)

        assert len(merged) == 1, "同域名只应保留一条"
        assert dropped == 1
        assert merged[0].site_url == "https://dup.com/"
        assert merged[0].feed_url is not None, "feed 地址不该丢"

    def test_html_markup_cleaned(self):
        """text 里的 HTML 标记和实体要清理掉。"""
        from app.services.opml_parser import parse_opml

        result = parse_opml(HTML_IN_TEXT)
        entry = result.entries[0]
        assert "<b>" not in entry.name
        assert "&lt;" not in entry.name
        assert "加粗标题" in entry.name

    def test_opml_1_0_supported(self):
        from app.services.opml_parser import parse_opml

        result = parse_opml(OPML_1_0)
        assert len(result.entries) == 1
        assert result.entries[0].name == "老站点"

    def test_bare_domain_gets_protocol(self):
        """OPML 里的裸域名要自动补协议，否则生成抓不通的站点。

        实测发现：导出器经常写出「www.example.com」而不带 https://。
        """
        from app.services.opml_parser import parse_opml

        bare = """<?xml version="1.0" encoding="UTF-8"?>
<opml version="2.0"><head><title>t</title></head><body>
<outline type="rss" text="裸域名" htmlUrl="bare.com"
         xmlUrl="https://bare.com/atom.xml"/>
</body></opml>"""
        entry = parse_opml(bare).entries[0]
        assert entry.site_url.startswith("https://"), "应自动补 https://"
        assert entry.site_url == "https://bare.com/"

    def test_invalid_xml_raises(self):
        from app.services.opml_parser import parse_opml

        with pytest.raises(ValueError, match="OPML|XML"):
            parse_opml("<html><body>不是 OPML</body></html>")

    def test_empty_body(self):
        from app.services.opml_parser import parse_opml

        result = parse_opml(
            '<?xml version="1.0"?><opml version="2.0">'
            "<head><title>空</title></head><body></body></opml>"
        )
        assert result.entries == []

    def test_gbk_encoding(self):
        """国内阅读器导出常见 GBK 编码，不能直接报错。"""
        from app.services.opml_parser import parse_opml

        opml = """<?xml version="1.0" encoding="GBK"?>
<opml version="2.0"><head><title>中文标题</title></head><body>
<outline type="rss" text="中文站点" htmlUrl="https://gbk.com/" xmlUrl="https://gbk.com/f.xml"/>
</body></opml>"""
        result = parse_opml(opml.encode("gbk"))
        assert result.entries[0].name == "中文站点"

    def test_deeply_nested_does_not_crash(self):
        """恶意/异常文件可能嵌套极深，不能栈溢出。"""
        from app.services.opml_parser import parse_opml

        deep = (
            '<?xml version="1.0"?><opml version="2.0"><head><title>d</title></head><body>'
            + "<outline text='层'>" * 200
            + "<outline type='rss' text='深处' htmlUrl='https://deep.com/' xmlUrl='https://deep.com/f'/>"
            + "</outline>" * 200
            + "</body></opml>"
        )
        result = parse_opml(deep)  # 不抛异常即通过
        assert isinstance(result.entries, list)


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


class TestImport:
    @pytest.mark.asyncio
    async def test_imports_new_sites(self, db):
        from app.core.tenant import set_tenant
        from app.services.opml_import import import_opml

        set_tenant(1)
        result = await import_opml(FRESH_RSS_NESTED, 1, db)

        assert result["created"] == 3
        assert result["skipped_existing"] == 0

        from sqlalchemy import select

        from app.models import Site

        sites = (await db.execute(select(Site))).scalars().all()
        assert len(sites) == 3
        assert all(s.source == "opml" for s in sites)
        # feed 地址要写进去，这样抓取时能省掉 RSS 探测
        assert any(s.feed_url == "https://akilar.top/atom.xml" for s in sites)
        set_tenant(None)

    @pytest.mark.asyncio
    async def test_reimport_skips_existing(self, db):
        """重复导入同一份文件不该产生重复站点。"""
        from app.core.tenant import set_tenant
        from app.services.opml_import import import_opml

        set_tenant(1)
        first = await import_opml(FRESH_RSS_NESTED, 1, db)
        second = await import_opml(FRESH_RSS_NESTED, 1, db)

        assert first["created"] == 3
        assert second["created"] == 0
        assert second["skipped_existing"] == 3

        from sqlalchemy import select

        from app.models import Site

        assert len((await db.execute(select(Site))).scalars().all()) == 3
        set_tenant(None)

    @pytest.mark.asyncio
    async def test_blacklist_filtered(self, db):
        """黑名单里的站点不该被导入。"""
        from app.core.tenant import set_tenant
        from app.models import Blacklist
        from app.services.opml_import import import_opml

        set_tenant(1)
        db.add(Blacklist(user_id=1, pattern="akilar.top"))
        await db.commit()

        result = await import_opml(FRESH_RSS_NESTED, 1, db)
        assert result["created"] == 2, "被拉黑的站点应被跳过"
        assert result["skipped_blacklist"] == 1
        set_tenant(None)

    @pytest.mark.asyncio
    async def test_respects_max_sites(self, db):
        """max_sites 必须真的生效，否则几百个站点的导入会卡死。"""
        from app.core.tenant import set_tenant
        from app.services.opml_import import import_opml

        set_tenant(1)
        result = await import_opml(FRESH_RSS_NESTED, 1, db, max_sites=1)

        assert result["created"] == 1
        set_tenant(None)

    @pytest.mark.asyncio
    async def test_inferred_flagged_for_review(self, db):
        """从 feed 推断的站点 URL 要标出来让用户核对。"""
        from app.core.tenant import set_tenant
        from app.services.opml_import import import_opml

        set_tenant(1)
        result = await import_opml(FEED_ONLY, 1, db)

        assert result["created"] == 2
        assert result["pending_review_count"] == 2, "推断的站点应提示确认"
        set_tenant(None)

    @pytest.mark.asyncio
    async def test_inferred_can_be_excluded(self, db):
        """用户可以选择不导入推断出来的条目。"""
        from app.core.tenant import set_tenant
        from app.services.opml_import import import_opml

        set_tenant(1)
        result = await import_opml(FEED_ONLY, 1, db, include_inferred=False)

        assert result["created"] == 0
        set_tenant(None)

    @pytest.mark.asyncio
    async def test_import_isolated_between_users(self, db):
        """Alice 导入的站点，Bob 不能导入时当成自己已有的。"""
        from sqlalchemy import select

        from app.core.tenant import as_tenant_async, set_tenant
        from app.models import Site
        from app.services.opml_import import import_opml

        set_tenant(1)
        await import_opml(FRESH_RSS_NESTED, 1, db)

        # Alice 有 3 个站点
        alice_sites = (await db.execute(select(Site))).scalars().all()
        assert len(alice_sites) == 3

        # Bob 导入同一份文件，应该能创建自己的 3 个
        async with as_tenant_async(2):
            bob_result = await import_opml(FRESH_RSS_NESTED, 2, db)
            assert bob_result["created"] == 3, "Bob 不该被 Alice 的数据挡住"
        set_tenant(None)