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
# 常见 feed 路径。兜底用——页面里没声明 <link rel="alternate"> 时盲猜。
#
# 顺序有意义：越常见的越靠前，先命中就能少发几个请求。
# 注意带不带 www 由调用方决定：这里都是相对路径，
# 调用方用 urljoin 拼上基准地址，保留原域的 www 前缀。
#
# 关于末尾斜杠：调用方会为「目录型」路径自动补一个带斜杠的变体，
# 且**带斜杠的排前面**——很多站点的 /feed 会 301 跳到 /feed/，
# 而 /feed/ 才是权威地址；也有站点只认带斜杠的那个。
# 所以这里只列不带斜杠的形态，斜杠变体由调用方生成。
COMMON_FEED_PATHS = (
    "/feed",
    "/feed/rss",
    "/rss.xml",
    "/feed.xml",
    "/atom.xml",
    "/index.xml",
    "/rss",
    "/atom",
    "/blog/feed",
    "/feeds/posts/default",  # Blogger
    "/rss/index.xml",       # 各类 CMS 变体
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
                guid=_extract_guid(entry, link),
                content=_extract_content(entry),
            )
        )

    return site if site.posts else None


def _extract_content(entry) -> str | None:
    """取 RSS 条目里的全文。

    WordPress 的 feed 默认带 content:encoded（全文 HTML）。
    对「文章页是 SPA 骨架页」的站点，这是唯一能拿到正文的途径——
    那些站返回的 HTML 只有一两 KB，正文全靠 JS 渲染。

    只认 content:encoded（全文），不认 summary（那是摘要，通常只有几十字）。
    """
    contents = entry.get("content") or []
    if isinstance(contents, list):
        for item in contents:
            value = item.get("value") if isinstance(item, dict) else None
            if value and len(value) > 200:
                return value
    return None


def _extract_guid(entry, fallback_link: str) -> str | None:
    """从 feed 条目里取 guid。

    WordPress 的 guid 是 `https://site/?p=12345`——文章 ID，
    跟永久链接结构、域名都无关。博主改 permalink 结构（/2026/10/2281/
    换成 /post/2281/）或换域名（.com 换 .cn）时，guid 保持不变，
    用它做去重键才不会把同一篇文章当成多篇。

    取不到就返回 None，让调用方回退到按 link 去重。
    """
    raw = entry.get("id") or entry.get("guid") or ""
    if not raw:
        return None
    raw = str(raw).strip()
    if not raw:
        return None

    # guid 常常就是 permalink 本身（isPermaLink=true），
    # 那种情况下它跟 link 一样不稳定，交给调用方按 link 处理。
    if _strip_scheme(raw) == _strip_scheme(fallback_link):
        return None

    # WordPress 的 ?p=12345 形态：只留 ID 部分，域名无关
    if "?p=" in raw:
        tail = raw.split("?p=", 1)[1].split("&", 1)[0].strip()
        if tail.isdigit():
            return f"wp:{tail}"

    return raw


def _strip_scheme(url: str) -> str:
    """去掉协议和 www.，用于比较两个 URL 是否指向同一篇。"""
    without_scheme = url.split("://", 1)[-1]
    without_scheme = without_scheme.removeprefix("www.")
    return without_scheme.rstrip("/").lower()


def _to_datetime(time_tuple) -> datetime | None:
    if not time_tuple:
        return None
    try:
        return datetime(*time_tuple[:6], tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None