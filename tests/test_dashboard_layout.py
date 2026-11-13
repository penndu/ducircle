"""概览页「最新文章」摘要不出框。

问题复现：概览页宽屏（≥1400px）是双列 grid，每列只有 650~750px。
摘要原来用单行省略（white-space:nowrap + text-overflow:ellipsis），
64 个中文字（12px 字号）需要约 770px——放不下就横向溢出，
压到相邻内容上，用户看到的是「摘要内容出框」。

修法：改用两行截断（-webkit-line-clamp），文字正常换行，
任何视口宽度都不会溢出。

这里做的是 CSS 层面的静态断言（CI 里不依赖浏览器）；
真实渲染验证见 tests/visual/check_overflow.py（需要 Playwright）。
"""

import re
from pathlib import Path

DASHBOARD = Path("web/templates/dashboard.html")


def _css_block() -> str:
    return DASHBOARD.read_text(encoding="utf-8")


def _rule(name: str) -> str:
    """取出一条 CSS 规则（含多行）。"""
    css = _css_block()
    match = re.search(rf"\.{re.escape(name)}\s*\{{(.*?)\}}", css, re.S)
    assert match, f"没找到 .{name} 的样式"
    return match.group(1)


class TestSummaryNoOverflow:
    def test_summary_not_nowrap(self):
        """摘要不能是单行 nowrap——那正是出框的原因。"""
        rule = _rule("ds-post-sum")
        assert "white-space:nowrap" not in rule, (
            "摘要用 white-space:nowrap 会在窄列里横向溢出"
        )

    def test_summary_uses_line_clamp(self):
        """摘要用 line-clamp 限制在两行内。"""
        rule = _rule("ds-post-sum")
        assert "-webkit-line-clamp" in rule
        assert "-webkit-box-orient:vertical" in rule

    def test_summary_keeps_overflow_hidden(self):
        """超出两行的部分要裁掉。"""
        rule = _rule("ds-post-sum")
        assert "overflow:hidden" in rule

    def test_summary_can_wrap_long_words(self):
        """长英文/URL 不能撑破容器。"""
        rule = _rule("ds-post-sum")
        assert "word-break" in rule or "overflow-wrap" in rule

    def test_title_still_ellipsis(self):
        """标题保持单行省略——标题最长 24 字，放得下。"""
        rule = _rule("ds-post-title")
        assert "white-space:nowrap" in rule
        assert "text-overflow:ellipsis" in rule

    def test_post_body_has_min_width_zero(self):
        """flex 子项要能收缩，否则内容会撑破卡片。"""
        rule = _rule("ds-post-body")
        assert "min-width:0" in rule


class TestGridColumns:
    def test_two_column_layout_uses_minmax_zero(self):
        """双列 grid 必须用 minmax(0,1fr)。

        写成 1fr 1fr 时，grid item 的默认 min-width:auto 会让
        内容把列撑宽——这是「双列下才出框」的根本原因。
        """
        css = _css_block()
        match = re.search(
            r"\.ds-posts\{grid-template-columns:repeat\(2,minmax\(([^)]*)\)", css
        )
        assert match, "没找到双列布局规则"
        # minmax 的第一个参数必须是 0，否则内容会撑破列
        assert match.group(1).split(",")[0].strip() == "0"


class TestTemplateRender:
    def test_summary_rendered(self):
        """模板里确实渲染了摘要行。"""
        css = _css_block()
        assert 'class="ds-post-sum"' in css
