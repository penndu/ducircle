"""友链页解析与采集测试。

分两部分：
1. 解析器——用构造的 HTML 测各种页面结构
2. 真实页面——用抓下来的 dusays.com/friends/ 验证

真实页面的 HTML 存在 tests/fixtures/ 下。
如果文件不在，相关测试会跳过（不影响 CI）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.parser.link_page import parse_link_page

FIXTURES = Path(__file__).parent / "fixtures"


class TestParseLinkPage:
    """解析器的基本行为。"""

    def test_simple_card_list(self):
        html = """
        <html><body>
          <div class="friends">
            <div class="friend-card">
              <a href="https://alice.com"><img src="https://alice.com/a.png">Alice 的博客</a>
            </div>
            <div class="friend-card">
              <a href="https://bob.com">Bob</a>
            </div>
          </div>
        </body></html>
        """
        entries = parse_link_page(html, "https://example.com/friends/")
        urls = {e.url for e in entries}
        assert "https://alice.com" in urls
        assert "https://bob.com" in urls

    def test_excludes_internal_links(self):
        """站内导航不能当成友链。"""
        html = """
        <html><body>
          <div class="friend-list">
            <a href="https://real-friend.com">真朋友</a>
            <a href="/about/">关于我</a>
            <a href="/tags/">标签</a>
            <a href="https://example.com/archives/">归档</a>
          </div>
        </body></html>
        """
        entries = parse_link_page(html, "https://example.com/friends/")
        assert len(entries) == 1
        assert entries[0].url == "https://real-friend.com"

    def test_excludes_social_and_tool_links(self):
        """社交链接和工具站不算友链。"""
        html = """
        <html><body>
          <div class="friend-list">
            <a href="https://real-friend.com">真朋友</a>
            <a href="https://github.com/someone">GitHub</a>
            <a href="https://twitter.com/someone">推特</a>
            <a href="https://beian.miit.gov.cn">备案</a>
          </div>
        </body></html>
        """
        entries = parse_link_page(html, "https://example.com/friends/")
        assert [e.url for e in entries] == ["https://real-friend.com"]

    def test_relative_urls_resolved(self):
        """相对链接要转成绝对地址。

        注意：同域的相对链接会被当成站内导航过滤掉——
        友链页上的友链都是别人的站，应该是绝对外链。
        """
        html = """
        <html><body>
          <div class="friend-list">
            <a href="/friend/one">一号</a>
          </div>
        </body></html>
        """
        # 同域相对链接 -> 站内导航，过滤
        assert parse_link_page(html, "https://example.com/friends/") == []

        # 跨域相对链接（协议相对）-> 保留并补全
        html2 = """
        <html><body>
          <div class="friend-list">
            <a href="//other.com/friend/one">一号</a>
          </div>
        </body></html>
        """
        entries = parse_link_page(html2, "https://example.com/friends/")
        assert entries[0].url == "https://other.com/friend/one"

    def test_deduplicates_by_host(self):
        """同一个域名只保留一条。"""
        html = """
        <html><body>
          <div class="friend-list">
            <a href="https://same.com/page1">同站 A</a>
            <a href="https://same.com/page2">同站 B</a>
          </div>
        </body></html>
        """
        entries = parse_link_page(html, "https://example.com/friends/")
        assert len(entries) == 1

    def test_name_fallback_to_host(self):
        """没名字时用域名兜底。"""
        html = """
        <html><body>
          <div class="friend-list">
            <a href="https://nameless.com/"><img src="https://x.com/a.png"></a>
          </div>
        </body></html>
        """
        entries = parse_link_page(html, "https://example.com/friends/")
        assert entries[0].name == "nameless.com"

    def test_strips_icon_characters(self):
        """名称里的图标字符要去掉。"""
        html = """
        <html><body>
          <div class="friend-list">
            <a href="https://blog.acme.com"><i class="fas fa-comment"></i> 某某博客</a>
          </div>
        </body></html>
        """
        entries = parse_link_page(html, "https://example.com/friends/")
        assert "fa-comment" not in entries[0].name
        assert "某某博客" in entries[0].name

    def test_empty_page_returns_empty(self):
        html = "<html><body><p>这里什么都没有</p></body></html>"
        assert parse_link_page(html, "https://example.com/friends/") == []

    def test_ignores_javascript_and_anchor(self):
        html = """
        <html><body>
          <div class="friend-list">
            <a href="javascript:void(0)">点我</a>
            <a href="#top">回到顶部</a>
            <a href="https://ok.com">正常</a>
          </div>
        </body></html>
        """
        entries = parse_link_page(html, "https://example.com/friends/")
        assert [e.url for e in entries] == ["https://ok.com"]

    def test_fallback_scans_whole_page(self):
        """没有明显的友链容器时，退回全页扫描。"""
        html = """
        <html><body>
          <div class="something-else">
            <a href="https://friend-one.com">好友一</a>
            <a href="https://friend-two.com">好友二</a>
          </div>
        </body></html>
        """
        entries = parse_link_page(html, "https://example.com/friends/")
        urls = {e.url for e in entries}
        assert "https://friend-one.com" in urls
        assert "https://friend-two.com" in urls


class TestRealLinkPage:
    """真实页面的验证。用抓下来的 dusays.com/friends/。"""

    @pytest.fixture
    def real_html(self):
        path = FIXTURES / "dusays-friends.html"
        if not path.exists():
            pytest.skip("没有真实页面 fixture")
        return path.read_text(encoding="utf-8")

    def test_extracts_expected_count(self, real_html):
        """真实页面上有 50 张友链卡片。

        页面里站外链接有 59 个——多出来的 9 个是页脚推荐位、
        备案号、GitHub 链接，这些必须被过滤掉。
        """
        entries = parse_link_page(real_html, "https://dusays.com/friends/")
        assert len(entries) == 50, f"期望 50 条，实际 {len(entries)} 条"

    def test_all_entries_have_names(self, real_html):
        entries = parse_link_page(real_html, "https://dusays.com/friends/")
        assert all(e.name for e in entries), "每条友链都应该有名称"

    def test_no_noise_hosts(self, real_html):
        """备案号、GitHub 这类不能混进来。"""
        entries = parse_link_page(real_html, "https://dusays.com/friends/")
        hosts = {e.host.lower() for e in entries}
        for noise in ("github.com", "beian.miit.gov.cn", "creativecommons.org"):
            assert not any(noise in h for h in hosts), f"混入了噪音：{noise}"

    def test_avatars_extracted(self, real_html):
        """大部分友链应该有头像。"""
        entries = parse_link_page(real_html, "https://dusays.com/friends/")
        with_avatar = sum(1 for e in entries if e.avatar)
        assert with_avatar >= 40, f"只有 {with_avatar} 条提取到头像"

    def test_known_friend_present(self, real_html):
        """抽查几个已知友链。"""
        entries = parse_link_page(real_html, "https://dusays.com/friends/")
        hosts = {e.host.lower().replace("www.", "") for e in entries}
        assert "xaoxuu.com" in hosts
        assert "numden.com" in hosts
