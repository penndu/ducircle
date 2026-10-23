"""第 1 层：RSS / Atom 自动发现。

这是命中率最高的一层。实测抽样 3/3 成功，且拿到的条目质量最好——
结构化 feed 天然带标题、链接、时间，比从HTML 里猜选择器可靠得多。

发现顺序：HTML <link rel="alternate"> 声明 → 常见路径逐个探测。
"""

from __future__ import annotations

from datetime import datetime, timezone
from urllib.parse import urljoin

import feedparser

from app.parser.base import ParsedPost, ParsedSite

# 按成功率和大小排序的常见 feed 路径
COMMON_FEED_PATHS = (
    "/feed",
    "/atom.xml",
    "/index.xml",
    "/rss.xml",
    "/feed.xml",
    "/rss",
)

FEED_MIME_TYPES = (
    "application/rss+xml",
    "application/atom+xml",
    "application/xml",
    "text/xml",
)


def discover_feed_url(html: str, base_url: str) -> str | None:
    """从 HTML 的 <link rel="alternate"> 中找 feed 声明。

    静态博客生成器几乎都会输出这个声明，比盲猜路径准得多。
    """
    try:
        from app.parser.html import HTMLParser
    except ImportError:  # pragma: no cover
        return None

    tree = HTMLParser(html)
    for node in tree.css("link"):
        rel = (node.attributes.get("rel") or "").lower()
        type_ = (node.attributes.get("type") or "").lower()
        if "alternate" not in rel:
            continue
        if type_ and not any(t in type_ for t in ("rss", "atom", "xml")):
            continue
        href = node.attributes.get("href")
        if href:
            return urljoin(base_url, href)
    return None


def parse_feed(content: bytes, feed_url: str, base_url: str) -> ParsedSite | None:
    """解析 feed 内容。解析失败或无条目时返回 None，交由上层降级。"""
    parsed = feedparser.parse(content)
    entries = getattr(parsed, "entries", None)
    if not entries:
        return None

    site = ParsedSite(
        url=base_url,
        feed_url=feed_url,
        strategy="feed",
        framework="feed",
    )

    if getattr(parsed, "feed", None):
        site.name = (parsed.feed.get("title") or "").strip()
        site.description = (parsed.feed.get("subtitle") or "").strip() or None

    for entry in entries:
        link = entry.get("link") or ""
        if not link:
            # Atom 的 links 列表是备选来源
            for candidate in entry.get("links", []) or []:
                if candidate.get("rel") == "alternate":
                    link = candidate.get("href", "")
                    if link:
                        break
        if not link:
            continue

        link = urljoin(base_url, link)
        created = _to_datetime(entry.get("published_parsed") or entry.get("updated_parsed"))
        updated = _to_datetime(entry.get("updated_parsed"))

        site.posts.append(
            ParsedPost(
                title=entry.get("title", ""),
                link=link,
                created=created or updated,
                updated=updated or created,
                strategy="feed",
            )
        )

    return site if site.posts else None


def _to_datetime(time_tuple) -> datetime | None:
    if not time_tuple:
        return None
    try:
        return datetime(*time_tuple[:6], tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None