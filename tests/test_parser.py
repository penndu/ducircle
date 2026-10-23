"""解析器测试。用真实抓取的 HTML 片段做回归，防止规则改坏。"""

from datetime import datetime

import pytest

from app.core.security import guid_hash, normalize_url, site_hash
from app.parser.feed import discover_feed_url, parse_feed
from app.parser.fingerprint import (
    detect_framework,
    parse_datetime_flexible,
    parse_with_framework,
)
from app.parser.heuristic import looks_like_article, parse_with_heuristic

# ---- 真实站点特征片段 ----

HEXO_HOME = """
<!DOCTYPE html><html><head>
<meta name="generator" content="Hexo 6.2.0">
<meta name="description" content="Akilar的糖果屋">
<link rel="alternate" type="application/atom+xml" title="Akilar" href="/atom.xml">
</head><body>
<img class="site-avatar" src="/images/head.png">
<article class="article article-type-post">
  <div class="article-inner">
    <header class="article-header">
      <h1 class="article-title"><a class="headerlink" href="/2026/01/02/hello/">关于群聊更换的紧急通知</a></h1>
      <time datetime="2026-01-02T10:30:00.000Z"></time>
    </header>
  </div>
</article>
<article class="article article-type-post">
  <div class="article-inner">
    <h1 class="article-title"><a class="headerlink" href="/2026/01/01/post/">源计划-方舟：友链卡片</a></h1>
    <time datetime="2026-01-01T08:00:00.000Z"></time>
  </div>
</article>
</body></html>
"""

HUGO_HOME = """
<!DOCTYPE html><html><head>
<meta name="generator" content="Hugo 0.119.0">
</head><body>
<article class="post">
  <header><h2><a href="/posts/2026/think-like-wormhole/">思考就像虫洞</a></h2>
  <time datetime="2026-02-01">2026-02-01</time></header>
</article>
<article class="post">
  <header><h2><a href="/posts/2025/year-review/">2025年概览</a></h2>
  <time datetime="2025-12-31">2025-12-31</time></header>
</article>
</body></html>
"""

ATOM_FEED = """<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Black Flies</title>
  <subtitle>一个 Hexo 博客</subtitle>
  <entry>
    <title>家家争唱饮水词，纳兰心事几人知</title>
    <link href="https://example.com/2026/03/01/ci/"/>
    <published>2026-03-01T12:00:00Z</published>
    <updated>2026-03-02T08:00:00Z</updated>
  </entry>
  <entry>
    <title>近日随想</title>
    <link href="/2026/02/20/thoughts/"/>
    <published>2026-02-20T09:00:00Z</published>
  </entry>
</feed>
"""


class TestNormalizeUrl:
    """URL 规范化直接影响去重准确率，逐个场景验证。"""

    def test_strips_www(self):
        assert normalize_url("https://www.example.com/a/") == "https://example.com/a"

    def test_strips_tracking_params(self):
        actual = normalize_url("https://example.com/p?utm_source=x&id=5&from=weibo")
        assert actual == "https://example.com/p?id=5"

    def test_http_upgraded_to_https(self):
        assert normalize_url("http://example.com/x").startswith("https://")

    def test_fragment_removed(self):
        assert normalize_url("https://example.com/p#section") == "https://example.com/p"

    def test_relative_url_joined(self):
        actual = normalize_url("/2026/01/", "https://example.com")
        assert actual == "https://example.com/2026/01"

    def test_guid_hash_identical_for_tracked_url(self):
        """同一篇文章带 utm 参数分享，必须识别为同一篇。"""
        a = guid_hash("https://example.com/post?utm_source=weibo")
        b = guid_hash("https://www.example.com/post/")
        assert a == b

    def test_site_hash_ignores_path(self):
        """同站不同页面应指向同一站点。"""
        assert site_hash("https://example.com/a") == site_hash("https://example.com/b/c")


class TestFingerprint:
    def test_detect_hexo(self):
        assert detect_framework(HEXO_HOME) == "hexo"

    def test_detect_hugo(self):
        assert detect_framework(HUGO_HOME) == "hugo"

    def test_detect_wordpress_via_body_class(self):
        html = '<html><head></head><body class="wp-site-blocks"></body></html>'
        assert detect_framework(html) == "wordpress"

    def test_detect_unknown_returns_none(self):
        assert detect_framework("<html><body>纯手写页面</body></html>") is None

    def test_parse_hexo_posts(self):
        site = parse_with_framework(HEXO_HOME, "https://akilar.top", "hexo")
        assert site is not None
        assert len(site.posts) == 2
        assert site.posts[0].title == "关于群聊更换的紧急通知"
        assert site.posts[0].link == "https://akilar.top/2026/01/02/hello"

    def test_parse_hugo_posts(self):
        site = parse_with_framework(HUGO_HOME, "https://elizen.me", "hugo")
        assert site is not None
        assert len(site.posts) == 2
        assert site.framework == "hugo"

    def test_avatar_extracted(self):
        site = parse_with_framework(HEXO_HOME, "https://akilar.top", "hexo")
        assert site.avatar == "/images/head.png"


class TestDateParsing:
    @pytest.mark.parametrize(
        "raw,expected_year",
        [
            ("2026-01-02", 2026),
            ("2026/01/02", 2026),
            ("2026年01月02日", 2026),
            ("2026-01-02T10:30:00Z", 2026),
        ],
    )
    def test_common_formats(self, raw, expected_year):
        assert parse_datetime_flexible(raw).year == expected_year

    def test_two_digit_year(self):
        """两位年份是博客主题常见坑，按 20xx 处理。"""
        assert parse_datetime_flexible("26-01-02").year == 2026

    def test_unix_timestamp(self):
        assert parse_datetime_flexible("1767327000").year == 2026

    def test_invalid_returns_none(self):
        assert parse_datetime_flexible("不是日期") is None

    def test_empty_returns_none(self):
        assert parse_datetime_flexible("") is None


class TestFeed:
    def test_discover_from_link_tag(self):
        url = discover_feed_url(HEXO_HOME, "https://akilar.top")
        assert url == "https://akilar.top/atom.xml"

    def test_parse_atom_entries(self):
        site = parse_feed(ATOM_FEED.encode(), "https://example.com/atom.xml",
                          "https://example.com")
        assert site is not None
        assert len(site.posts) == 2
        assert site.strategy == "feed"

    def test_relative_link_resolved(self):
        site = parse_feed(ATOM_FEED.encode(), "https://example.com/atom.xml",
                          "https://example.com")
        links = [p.link for p in site.posts]
        assert "https://example.com/2026/02/20/thoughts/" in links

    def test_empty_feed_returns_none(self):
        assert parse_feed(b"<feed></feed>", "x", "y") is None


class TestHeuristic:
    def test_filters_archive_pages(self):
        """归档页/标签页不能混进文章流。"""
        assert looks_like_article("https://example.com/2026/01/post/") is True
        assert looks_like_article("https://example.com/2026/07/") is False
        assert looks_like_article("https://example.com/tags/python") is False
        assert looks_like_article("https://example.com/archives/2026") is False
        assert looks_like_article("https://example.com/") is False

    def test_extract_from_article_tag(self):
        html = """
        <div>
          <article><h2><a href="/2026/01/a/">文章一</a>
            <time datetime="2026-01-01">2026-01-01</time></article>
          <article><h2><a href="/2026/01/b/">文章二</a>
            <time datetime="2026-01-02">2026-01-02</time></article>
          <article><h2><a href="/2026/01/c/">文章三</a>
            <time datetime="2026-01-03">2026-01-03</time></article>
        </div>
        """
        site = parse_with_heuristic(html, "https://example.com")
        assert site is not None
        assert len(site.posts) == 3
        assert site.strategy == "heuristic"

    def test_too_few_items_rejected(self):
        """只有 1 条时不确定结构是否正确，宁可漏也不误。"""
        html = '<article><h2><a href="/a/">唯一一条</a></h2></article>'
        assert parse_with_heuristic(html, "https://example.com") is None

    def test_navigation_links_excluded(self):
        html = """
        <div>
          <article><h2><a href="/2026/1/a/">A</a><time>2026-01-01</time></article>
          <article><h2><a href="/2026/1/b/">B</a><time>2026-01-02</time></article>
          <article><h2><a href="/2026/1/c/">C</a><time>2026-01-03</time></article>
          <a href="/tags/python">标签云</a>
          <a href="/archives/2026/">归档</a>
        </div>
        """
        site = parse_with_heuristic(html, "https://example.com")
        assert site is not None
        links = [p.link for p in site.posts]
        assert not any("/tags/" in link for link in links)
        assert not any("/archives/" in link for link in links)