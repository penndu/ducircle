"""第 3 层：通用结构推断。

框架指纹认不出来时的兜底。不猜主题，只找 HTML 里通用的语义结构：
<article> / <time> / 重复的标题链接模式。

这套规则宁可漏也不误——只信任结构上足够明确的模式，
避免把导航链接、标签页链接当成文章混进文章流。
"""

from __future__ import annotations

import re
from urllib.parse import urljoin

from app.core.security import normalize_url
from app.parser.base import ParsedPost, ParsedSite
from app.parser.fingerprint import parse_datetime_flexible

# 链接容器候选。要求容器内至少包含标题与日期，命中才算一条文章。
CONTAINER_SELECTORS = (
    "article",
    "li.post-item",
    "li.entry",
    "li.article",
    ".post-card",
    ".post-item",
    ".entry",
    ".article-item",
    ".blog-list-item",
    ".post-list-item",
)

# 标题链接候选。相对 href 必须以 / 开头或含年份/日期片段，
# 用来排除分类页、标签页、归档页等非文章链接。
TITLE_SELECTORS = (
    "h1 a",
    "h2 a",
    "h3 a",
    "h4 a",
    ".title a",
    ".post-title a",
    ".entry-title a",
    ".article-title a",
)

DATE_SELECTORS = ("time[datetime]", "time", "[datetime]", ".date", ".post-date")

# 归档页通常是 /2026/07/ 这类短路径
ARCHIVE_PATH = re.compile(r"^/\d{4}/?\d{0,2}/?$")
# 非文章路径：标签、分类、归档、关于、分页等。
# 注意 /tags/python 这类标签后面跟的是任意 slug 而非数字，
# 所以不能只匹配数字结尾。
TAG_PATH = re.compile(
    r"^/(tag|tags|category|categories|archives?|about|page|search|author|sitemap)"
    r"(/.*)?$",
    re.I,
)


def looks_like_article(url: str) -> bool:
    """过滤掉明显不是文章的链接。"""
    from urllib.parse import urlsplit

    parts = urlsplit(url)
    path = parts.path or "/"

    # 无路径或根路径
    if path in ("", "/"):
        return False

    # Typecho 等老程序用 /12345.html 做永久链接，.html 是文章而非静态资源，
    # 因此只排除资源类扩展名。
    if re.search(
        r"\.(png|jpe?g|gif|webp|svg|css|js|json|xml|zip|rar|7z|pdf|mp4|mp3)$",
        path, re.I,
    ):
        return False

    if ARCHIVE_PATH.match(path):
        return False
    if TAG_PATH.match(path):
        return False
    # 纯数字分页，如 /2/
    if re.fullmatch(r"/\d+/?", path):
        return False
    return True


def parse_with_heuristic(html: str, base_url: str) -> ParsedSite | None:
    from app.parser.html import HTMLParser

    tree = HTMLParser(html)
    seen: set[str] = set()

    # 逐个尝试容器选择器，命中数达标才采用。
    # 每个选择器独立收集，失败就丢弃自己的结果，不污染其他选择器。
    for container_selector in CONTAINER_SELECTORS:
        containers = tree.css(container_selector)
        if not containers:
            continue

        collected: list[ParsedPost] = []
        local_seen: set[str] = set()

        for container in containers:
            parsed = _extract_from_container(container, base_url)
            if parsed is None:
                continue
            link, title, created = parsed
            if link in seen or link in local_seen:
                continue
            local_seen.add(link)
            collected.append(
                ParsedPost(
                    title=title,
                    link=link,
                    created=created,
                    updated=created,
                    strategy="heuristic",
                )
            )

        # 至少 3 条才认为结构判断正确。少于 3 条更可能是误匹配
        #（比如导航里的 article 标签），宁可不收也不污染文章流。
        if len(collected) >= 3:
            seen |= local_seen
            return ParsedSite(
                url=base_url,
                strategy="heuristic",
                framework=None,
                posts=collected,
                name=_document_title(tree) or base_url,
                description=_meta_description(tree),
            )

    return None


def _extract_from_container(container, base_url: str):
    """从单个容器节点提取 (link, title, created)。结构不够明确则返回 None。"""
    href = title = None

    for selector in TITLE_SELECTORS:
        node = container.css_first(selector)
        if node is None:
            continue
        raw_href = node.attributes.get("href")
        if not raw_href:
            continue
        candidate = normalize_url(urljoin(base_url, raw_href))
        if not looks_like_article(candidate):
            continue
        text = node.text(strip=True)
        # 单字标题在中文博客里真实存在（如「书」「记」），不能用长度过滤。
        # 只挡空字符串和纯空白——真正的过滤交给 looks_like_article 的路径判断。
        if not text:
            continue
        href = candidate
        title = text
        break

    if href is None:
        return None

    created = None
    for selector in DATE_SELECTORS:
        node = container.css_first(selector)
        if node is None:
            continue
        raw = (
            node.attributes.get("datetime")
            or node.attributes.get("content")
            or node.text(strip=True)
        )
        if raw:
            created = parse_datetime_flexible(raw)
            if created:
                break

    return href, title, created


def _document_title(tree) -> str | None:
    node = tree.css_first("title")
    if node is None:
        return None
    text = node.text(strip=True)
    # 去掉 " - 站点名" 这类后缀，它往往已经是站点名了
    return re.split(r"\s*[|｜\-–—]\s*", text)[0].strip() or text


def _meta_description(tree) -> str | None:
    node = tree.css_first('meta[name="description"]')
    if node is None:
        return None
    return (node.attributes.get("content") or "").strip() or None