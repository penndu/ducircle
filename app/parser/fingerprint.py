"""第 2 层：框架指纹识别。

实测抽样 3/3 命中（Hexo 6.2.0 / Hugo 0.119.0 / Hexo 5.4.2）。

思路：不看视觉结构，只看生成器留下的稳定标记。同一框架的不同主题，
文章列表的 HTML 骨架是高度一致的——它们都由同一套模板渲染出来。
所以按框架写一次通用规则，就能覆盖该框架下所有主题，
而不用像旧工具那样为每个主题单独写选择器。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.core.security import normalize_url
from app.parser.base import ParsedPost, ParsedSite


@dataclass(frozen=True)
class FrameworkRule:
    """框架级通用规则。

    link_selectors 用于定位文章链接，title_selector 通常与 link 同节点，
    date_selectors 用于定位时间（可多个，按顺序尝试）。
    """

    name: str
    # 定位文章链接的候选选择器，按顺序尝试直到命中
    link_selectors: tuple[str, ...]
    # 日期选择器，可空
    date_selectors: tuple[str, ...] = ()
    # 站点信息选择器
    title_selectors: tuple[str, ...] = ()
    avatar_selectors: tuple[str, ...] = ()
    description_selectors: tuple[str, ...] = ()


# 各框架通用规则。选择器刻意避开主题定制类名，只用框架自身生成的稳定结构；
# 但纯框架不足以覆盖所有主题（实测 Ark 主题连<article> 标签都不用），
# 因此额外收录了社区中大量使用的通用列表类名作为兜底。
FRAMEWORK_RULES: dict[str, FrameworkRule] = {
    "hexo": FrameworkRule(
        name="hexo",
        # Hexo 默认输出结构；.post-link / article.post 类名在各主题间高度一致
        link_selectors=(
            "article a.headerlink",
            "article a.post-title-link",
            "article h1 a",
            "article h2 a",
            ".post-title a",
            "h2.article-title a",
            # Ark / Volantis 等主题的侧栏与卡片式列表
            ".aside-list-item a.title",
            ".post-card a[href*='/posts/']",
            ".post-item a[href*='/posts/']",
            # 无 h 标签的纯文本链接列表
            ".post-list a[href*='/posts/']",
            # anzhiyu 等主题的 recent-post-item 列表
            ".recent-post-item a.article-title",
            ".recent-post-item .article-title",
            # .html 永久链接（Typecho 及魔改 Hexo 主题）
            ".recent-post-item a[href$='.html']",
        ),
        date_selectors=(
            "time[datetime]",
            "time",
            ".post-meta time",
            ".article-meta time",
        ),
        avatar_selectors=(".site-avatar", "img.site-avatar", ".avatar img", "img.avatar"),
    ),
    "hugo": FrameworkRule(
        name="hugo",
        link_selectors=(
            "article h2 a",
            "article h3 a",
            ".post-title a",
            "h2.entry-title a",
            # Hugo 社区主题常用的卡片列表
            ".post-card a[href*='/posts/']",
            ".post-item a[href*='/post/']",
            "ul li a[href*='/posts/']",
        ),
        date_selectors=("time[datetime]", ".post-date time", "time"),
        avatar_selectors=(".site-avatar", "img.site-avatar", "img.avatar"),
    ),
    "wordpress": FrameworkRule(
        name="wordpress",
        link_selectors=(
            "article h2 a.entry-title",
            "article h2 a",
            "article h3 a",
            "h2.entry-title a",
            ".post-title a",
        ),
        date_selectors=("time[datetime]", ".entry-date", ".posted-on time"),
        avatar_selectors=(".site-avatar img", "img.avatar"),
    ),
    "vuepress": FrameworkRule(
        name="vuepress",
        link_selectors=(
            ".post article h2 a",
            ".theme-default-content h2 a",
            "h2 a",
        ),
        date_selectors=("time", ".page-meta time"),
    ),
    "vitepress": FrameworkRule(
        name="vitepress",
        link_selectors=(
            ".VPPost h2 a",
            ".vp-doc h2 a",
            "h2 a",
        ),
        date_selectors=("time", ".VPPostHeader time"),
    ),
    "astro": FrameworkRule(
        name="astro",
        link_selectors=("article h2 a", "article h3 a", ".post-title a"),
        date_selectors=("time[datetime]", "time"),
    ),
    "gatsby": FrameworkRule(
        name="gatsby",
        link_selectors=("article h2 a", "h3.gatsby-blog-post-title a"),
        date_selectors=("time", ".gatsby-highlight time"),
    ),
    "nuxt": FrameworkRule(
        name="nuxt",
        link_selectors=("article h2 a", ".article-title a", ".post-title a"),
        date_selectors=("time", ".article-date"),
    ),
    "zola": FrameworkRule(
        name="zola",
        link_selectors=("article header h1 a", "article h2 a"),
        date_selectors=("time", ".post-date"),
    ),
}

# meta generator 内容特征 → 框架名。按顺序匹配，第一个命中即返回。
GENERATOR_PATTERNS: tuple[tuple[str, re.Pattern], ...] = (
    ("hexo", re.compile(r"\bhexo\b", re.I)),
    ("hugo", re.compile(r"\bhugo\b", re.I)),
    ("wordpress", re.compile(r"wordpress", re.I)),
    ("vuepress", re.compile(r"vuepress", re.I)),
    ("vitepress", re.compile(r"vitepress", re.I)),
    ("astro", re.compile(r"\bastro\b", re.I)),
    ("gatsby", re.compile(r"\bgatsby\b", re.I)),
    ("nuxt", re.compile(r"\bnuxt\b|\bnext\.js\b|\bnextjs\b", re.I)),
    ("zola", re.compile(r"\bzola\b", re.I)),
)


def detect_framework(html: str) -> str | None:
    """从 HTML 中识别生成框架。

    依次检查 meta generator、html lang+注释标记、常见 JS 资源路径。
    """
    match = re.search(
        r'<meta[^>]+name=["\']generator["\'][^>]+content=["\']([^"\']+)["\']',
        html, re.I,
    )
    if match:
        content = match.group(1)
        for framework, pattern in GENERATOR_PATTERNS:
            if pattern.search(content):
                return framework

    # WordPress 常在 body class 里留 wp- 前缀
    if re.search(r'<body[^>]+class=["\'][^"\']*\bwp-', html, re.I):
        return "wordpress"

    # 静态生成器通常在 head 里带注释标记
    lowered = html[:5000].lower()
    if "<!doctype html>" in lowered and "hexo" in lowered:
        return "hexo"
    if "hugo" in lowered:
        return "hugo"

    return None


def parse_with_framework(
    html: str, base_url: str, framework: str
) -> ParsedSite | None:
    """按框架通用规则解析文章列表页。"""
    rule = FRAMEWORK_RULES.get(framework)
    if rule is None:
        return None

    from app.parser.html import HTMLParser
    from app.parser.heuristic import looks_like_article

    tree = HTMLParser(html)
    posts: list[ParsedPost] = []
    seen: set[str] = set()

    for selector in rule.link_selectors:
        for node in tree.css(selector):
            href = node.attributes.get("href")
            if not href:
                continue
            from urllib.parse import urljoin

            link = normalize_url(urljoin(base_url, href))
            # 归档页/标签页/根路径不是文章。卡片类选择器容易命中这类链接，
            # 不加这层过滤会把导航链接混进文章流。
            if not link or link in seen or not looks_like_article(link):
                continue

            title = node.text(strip=True) or ""
            if not title:
                # 卡片主题常把标题放在兄弟节点或图片的 alt 上
                title = (
                    node.attributes.get("aria-label")
                    or node.attributes.get("title")
                    or ""
                )
                if not title:
                    parent = node.parent
                    for sibling_sel in ("a.title", "h2", "h3", ".title", ".post-title"):
                        if parent is None:
                            break
                        sibling = parent.css_first(sibling_sel)
                        if sibling is not None:
                            title = sibling.text(strip=True)
                            if title:
                                break
            if not title:
                continue

            seen.add(link)
            created = _extract_date(tree, rule.date_selectors, node)
            posts.append(
                ParsedPost(
                    title=title,
                    link=link,
                    created=created,
                    updated=created,
                    strategy=f"fingerprint:{framework}",
                )
            )
        if len(posts) >= 3:
            break  # 命中足够数量即短路，不继续试后续选择器

    if not posts:
        return None

    site = ParsedSite(
        url=base_url,
        strategy=f"fingerprint:{framework}",
        framework=framework,
        posts=posts,
    )
    site.name = _document_site_name(tree) or base_url
    site.avatar = _first_attr(tree, rule.avatar_selectors, "src")
    site.description = _meta_description(tree)
    return site


def _document_site_name(tree) -> str | None:
    """取站点名。优先 og:site_name，其次 <title>。"""
    node = tree.css_first('meta[property="og:site_name"]')
    if node is not None:
        value = (node.attributes.get("content") or "").strip()
        if value:
            return value
    title = tree.css_first("title")
    if title is None:
        return None
    text = title.text(strip=True)
    # 去掉 " - 站点名" / " | 站点名" 这类后缀
    import re

    head = re.split(r"\s*[|｜\-–—]\s*", text)[0].strip()
    return head or text


def _extract_date(tree, selectors, anchor_node) -> object | None:
    """在文章节点或其最近祖先的文章块内找时间。"""
    from datetime import datetime

    # 先在链接节点自身及其最近的文章容器内找时间。
    # 直接全页搜索会拿到第一篇文章的日期套到所有文章上——这是常见错误。
    scopes = [anchor_node]
    parent = anchor_node.parent
    hops = 0
    while parent is not None and hops < 3:
        scopes.append(parent)
        parent = parent.parent
        hops += 1

    for scope in scopes:
        for selector in selectors:
            for node in scope.css(selector):
                raw = (
                    node.attributes.get("datetime")
                    or node.attributes.get("content")
                    or node.text(strip=True)
                )
                if not raw:
                    continue
                parsed = parse_datetime_flexible(raw)
                if parsed:
                    return parsed
    return None


def _first_text(tree, selectors) -> str | None:
    for selector in selectors:
        for node in tree.css(selector):
            text = node.text(strip=True)
            if text:
                return text
    return None


def _meta_description(tree) -> str | None:
    node = tree.css_first('meta[name="description"]')
    if node is None:
        return None
    return (node.attributes.get("content") or "").strip() or None


def _first_attr(tree, selectors, attr) -> str | None:
    for selector in selectors:
        for node in tree.css(selector):
            value = node.attributes.get(attr)
            if value:
                return value
    return None


# 支持的日期格式。博客主题的日期写法五花八门，
# 这里覆盖静态博客生成器实际会输出的格式。
# 注意：不收录 "%m月%d日" 这类缺年份的格式——Python 3.12+ 对它有
# 弃用警告，且没有默认年份本来就不可靠，宁可解析失败也不要猜。
DATE_FORMATS = (
    "%Y-%m-%dT%H:%M:%S%z",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%d",
    "%Y/%m/%d",
    "%Y年%m月%d日",
    "%b %d, %Y",
    "%d %b %Y",
    "%a, %d %b %Y %H:%M:%S %z",
)


def parse_datetime_flexible(raw: str) -> datetime | None:
    """宽松解析日期。两位年份按 2000+yy 处理（博客主题常见坑）。"""
    from datetime import datetime

    raw = (raw or "").strip()
    if not raw:
        return None

    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue

    # 纯数字时间戳
    if raw.isdigit() and len(raw) >= 9:
        ts = int(raw)
        if ts > 1e11:  # 毫秒级
            ts //= 1000
        try:
            return datetime.fromtimestamp(ts)
        except (ValueError, OSError, OverflowError):
            return None

    # 两位年份
    two_digit = re.match(r"^(\d{2})[-/](\d{1,2})[-/](\d{1,2})$", raw)
    if two_digit:
        y, m, d = two_digit.groups()
        try:
            return datetime(2000 + int(y), int(m), int(d))
        except ValueError:
            return None

    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None