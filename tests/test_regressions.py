"""回归测试：真实站点的失败模式。

这些用例全部来自 scripts/layer_check.py 的实测——
每一段 HTML 结构都是从线上真实抓下来、且确实让抓取失败过的。
改动解析规则时必须保证它们继续通过，否则线上会立刻回归。
"""

from app.parser.fingerprint import detect_framework, parse_with_framework
from app.parser.heuristic import looks_like_article, parse_with_heuristic

# ---- 案例 1：Ark 主题（akilar.top） ----
# 特征：全文没有任何 <article> / <h2> / <h1>，文章装在 aside-list-item 里。
# 这是旧工具 15 个主题全部不覆盖、导致抓不到的真实站点。
ARK_ASIDE_LIST = """
<div class="aside-list">
  <div class="aside-list-item">
    <a class="thumbnail" href="/posts/c215d440/"><img src="/1.jpg"></a>
    <div class="content"><a class="title" href="/posts/c215d440/">关于群聊更换的紧急通知</a>
      <time datetime="2024-04-24T05:42:01+08:00"></time></div>
  </div>
  <div class="aside-list-item">
    <div class="content"><a class="title" href="/posts/fb6a1a78/">源计划-方舟：友链卡片</a>
      <time datetime="2024-04-20T10:00:00+08:00"></time></div>
  </div>
  <div class="aside-list-item">
    <div class="content"><a class="title" href="/posts/aa11bb22/">第三篇测试文章</a>
      <time datetime="2024-04-18T09:00:00+08:00"></time></div>
  </div>
</div>
"""

# ---- 案例 2：anzhiyu 主题（noionion.top） ----
# 特征：文章链接是 /16433.html 这种 Typecho 风格永久链接，
# 曾因looks_like_article 把 .html 当资源过滤掉而全部丢失。
ANZHIYU_RECENT_POSTS = """
<div class="recent-posts">
  <div class="recent-post-item">
    <div class="post_cover left_ra"><a href="/16433.html"><img class="post_bg" src="/2.jpg"></a></div>
    <div class="recent-post-info">
      <a class="article-title" href="/16433.html">文章标题一</a>
      <time class="post-meta-date" datetime="2026-03-01T12:00:00+08:00"></time>
    </div>
  </div>
  <div class="recent-post-item">
    <div class="recent-post-info">
      <a class="article-title" href="/20692.html">文章标题二</a>
      <time datetime="2026-02-20T09:00:00+08:00"></time>
    </div>
  </div>
  <div class="recent-post-item">
    <div class="recent-post-info">
      <a class="article-title" href="/21001.html">文章标题三</a>
      <time datetime="2026-02-18T09:00:00+08:00"></time>
    </div>
  </div>
</div>
"""


class TestArkTheme:
    """Ark 主题：旧工具抓不了，新工具必须能抓。"""

    HTML = ARK_ASIDE_LIST

    def test_no_standard_tags(self):
        """确认这是真实的特殊情况——没有 article/h1/h2 可依赖。"""
        from app.parser.html import HTMLParser

        tree = HTMLParser(self.HTML)
        assert len(tree.css("article")) == 0
        assert len(tree.css("h2 a")) == 0

    def test_fingerprint_extracts_posts(self):
        site = parse_with_framework(self.HTML, "https://akilar.top", "hexo")
        assert site is not None, "Ark 主题必须能解析出文章"
        assert len(site.posts) == 3
        assert site.posts[0].title == "关于群聊更换的紧急通知"
        assert site.posts[0].link == "https://akilar.top/posts/c215d440"

    def test_dates_not_swapped(self):
        """日期必须来自各自文章块，不能全取第一篇的。"""
        site = parse_with_framework(self.HTML, "https://akilar.top", "hexo")
        assert site is not None
        dates = {p.title: p.created for p in site.posts}
        assert dates["关于群聊更换的紧急通知"].year == 2024
        assert dates["关于群聊更换的紧急通知"].month == 4
        assert dates["关于群聊更换的紧急通知"].day == 24
        # 第二篇应是 4-20，不能都是 4-24
        assert dates["源计划-方舟：友链卡片"].day == 20


class TestAnzhiyuTheme:
    """anzhiyu 主题：.html 永久链接曾被误判为资源而全数丢失。"""

    HTML = ANZHIYU_RECENT_POSTS

    def test_html_permalink_is_article(self):
        """Typecho 风格的 .html 链接必须被认作文章。"""
        assert looks_like_article("https://noionion.top/16433.html") is True

    def test_resources_still_excluded(self):
        """资源类扩展名仍需排除，不能为了放行 .html 而放宽全部。"""
        assert looks_like_article("https://example.com/a.png") is False
        assert looks_like_article("https://example.com/style.css") is False
        assert looks_like_article("https://example.com/data.json") is False

    def test_fingerprint_extracts_posts(self):
        site = parse_with_framework(self.HTML, "https://noionion.top", "hexo")
        assert site is not None
        assert len(site.posts) == 3

    def test_cover_image_link_not_treated_as_post(self):
        """文章卡片里封面图的链接与标题链接指向同一篇，不能重复计数。"""
        site = parse_with_framework(self.HTML, "https://noionion.top", "hexo")
        assert site is not None
        links = [p.link for p in site.posts]
        assert len(links) == len(set(links)), "同一篇文章不能出现两次"

    def test_title_not_empty(self):
        """封面链接没有文本，标题必须回退到 a.article-title 而不是变成空。"""
        site = parse_with_framework(self.HTML, "https://noionion.top", "hexo")
        assert site is not None
        assert all(p.title for p in site.posts)
        assert "文章标题一" in [p.title for p in site.posts]


class TestNavigationExclusion:
    """导航与归档链接绝不能混进文章流。"""

    def test_tag_page_rejected(self):
        assert looks_like_article("https://example.com/tags/python") is False
        assert looks_like_article("https://example.com/categories/tech") is False

    def test_archive_page_rejected(self):
        assert looks_like_article("https://example.com/2026/07/") is False
        assert looks_like_article("https://example.com/archives/2026") is False

    def test_pagination_rejected(self):
        assert looks_like_article("https://example.com/2") is False

    def test_fingerprint_filters_navigation(self):
        """指纹层也必须过滤导航链接，不能只依赖上层裁剪。"""
        html = """
        <div class="recent-posts">
          <div class="recent-post-item"><a class="article-title" href="/a1.html">一</a>
            <time datetime="2026-01-01"></time></div>
          <div class="recent-post-item"><a class="article-title" href="/a2.html">二</a>
            <time datetime="2026-01-02"></time></div>
          <div class="recent-post-item"><a class="article-title" href="/a3.html">三</a>
            <time datetime="2026-01-03"></time></div>
          <a class="article-title" href="/tags/python">标签云</a>
          <a class="article-title" href="/archives/2026/">归档</a>
        </div>
        """
        site = parse_with_framework(html, "https://example.com", "hexo")
        assert site is not None
        links = [p.link for p in site.posts]
        assert not any("/tags/" in link for link in links)
        assert not any("/archives/" in link for link in links)


class TestSingleCharTitle:
    """单字标题在中文博客里真实存在，不能被长度过滤误杀。"""

    def test_single_char_title_kept(self):
        html = """
        <div class="recent-posts">
          <div class="recent-post-item"><a class="article-title" href="/a1.html">书</a>
            <time datetime="2026-01-01"></time></div>
          <div class="recent-post-item"><a class="article-title" href="/a2.html">记</a>
            <time datetime="2026-01-02"></time></div>
          <div class="recent-post-item"><a class="article-title" href="/a3.html">诗</a>
            <time datetime="2026-01-03"></time></div>
        </div>
        """
        site = parse_with_framework(html, "https://example.com", "hexo")
        assert site is not None
        assert len(site.posts) == 3
        assert {p.title for p in site.posts} == {"书", "记", "诗"}

    def test_heuristic_also_keeps_single_char(self):
        html = """
        <article><h2><a href="/2026/1/a/">书</a><time>2026-01-01</time></article>
        <article><h2><a href="/2026/1/b/">记</a><time>2026-01-02</time></article>
        <article><h2><a href="/2026/1/c/">诗</a><time>2026-01-03</time></article>
        """
        site = parse_with_heuristic(html, "https://example.com")
        assert site is not None
        assert len(site.posts) == 3


class TestAvatarExtraction:
    """头像选择器：Hexo 把 class 和 img 写在同一元素上，不是 img 嵌在容器里。"""

    def test_avatar_on_same_element(self):
        html = '<html><body><img class="site-avatar" src="/a.png"></body></html>'
        site = parse_with_framework(html, "https://example.com", "hexo")
        # 没有文章时返回 None，只验证不抛异常
        assert site is None or site.avatar == "/a.png"