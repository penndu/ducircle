"""OPML 订阅列表解析。

OPML 是订阅器导出的标准格式（OPML 2.0 spec），各阅读器都支持导出。

**最关键的坑**：`xmlUrl` 是 feed 地址（/atom.xml），`htmlUrl` 才是站点主页。
很多人会拿 xmlUrl 当站点 URL，那样抓取必然失败——
抓到的是 feed XML，不是 HTML 页面。

本模块从 OPML 中提取：
- 站点 URL（优先 htmlUrl，没有则从 xmlUrl 的主机名推断）
- feed URL（xmlUrl，这是最有价值的信息——直接省掉 RSS 探测）
- 分类（嵌套的 outline 层级）

格式兼容性考虑各家导出器的差异：
- FreshRSS / TinyRSS / Feedly 用 title + htmlUrl + xmlUrl
- 老的 OPML 1.0 导出器可能没有 xmlUrl
- 嵌套分类在 FreshRSS 里是常见形态
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlsplit

from app.core.security import normalize_url, site_hash

logger = logging.getLogger("ducircle.opml")

# 单个文件的上限。恶意/损坏的文件可能嵌套极深或条目极多，
# 不限制会让解析占满内存。
MAX_OUTLINES = 2000
MAX_DEPTH = 10


@dataclass
class OpmlEntry:
    """一条 OPML 条目。"""

    name: str
    site_url: str
    feed_url: str | None = None
    category: str | None = None
    # 从 feed 地址推断出来的（当 htmlUrl 缺失时）
    inferred_site: bool = False


@dataclass
class OpmlResult:
    """解析结果。"""

    title: str = ""
    entries: list[OpmlEntry] = field(default_factory=list)
    # 被跳过的条目及原因，直接展示给用户
    skipped: list[tuple[str, str]] = field(default_factory=list)
    total_outlines: int = 0


def _clean_text(value: str | None) -> str:
    """清理 text/title 属性。

    OPML 规范允许 text 含 HTML 标记，实际导出器也确实会塞 <br> 之类。
    """
    if not value:
        return ""
    text = value.strip()
    # 去掉内嵌的 HTML 标签
    text = re.sub(r"<[^>]+>", "", text)
    # 反转义实体
    text = (
        text.replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", '"')
        .replace("&apos;", "'")
        .replace("&#39;", "'")
        .replace("&nbsp;", " ")
    )
    return re.sub(r"\s+", " ", text).strip()


def _infer_site_from_feed(feed_url: str) -> str | None:
    """只有 feed 地址时，推测站点主页。

    feed 通常在站点根目录或固定子路径下，反推根目录是最靠谱的猜测。
    但 /blog/feed.xml 这种子目录结构的站点猜不准，所以标注 inferred_site，
    让导入方知道这个 URL 可能需要用户确认。
    """
    parts = urlsplit(feed_url)
    if not parts.netloc:
        return None
    return f"{parts.scheme}://{parts.netloc}/"


def _normalize_site_url(raw: str) -> str | None:
    """规范化站点地址。

    OPML 里的 htmlUrl 理论上都带协议，但实际导出的文件里
    经常有「www.example.com」这种裸域名。与手动添加友链的行为保持一致：
    自动补 https://，避免生成一个抓不通的站点。
    """
    if not raw:
        return None
    value = raw.strip()
    if not value:
        return None
    if not value.startswith(("http://", "https://")):
        value = "https://" + value
    normalized = normalize_url(value)
    if not normalized.startswith(("http://", "https://")):
        return None
    return normalized


def parse_opml(content: str | bytes) -> OpmlResult:
    """解析 OPML 内容。

    用 xml.etree 而不是 BeautifulSoup——OPML 本来就是 XML，
    而且 ElementTree 能抵御 XXE 攻击（解析外部实体）。
    """
    import xml.etree.ElementTree as ET

    if isinstance(content, bytes):
        try:
            content = content.decode("utf-8")
        except UnicodeDecodeError:
            try:
                content = content.decode("gbk")
            except UnicodeDecodeError:
                raise ValueError("文件编码无法识别，请上传 UTF-8 的 OPML")

    # 有些导出器开头有 BOM 或空行，ElementTree 会报错
    content = content.lstrip("﻿").strip()
    if not content:
        raise ValueError("文件内容为空")

    try:
        root = ET.fromstring(content)
    except ET.ParseError as exc:
        raise ValueError(f"文件不是有效的 OPML/XML：{exc}") from exc

    # 有些文件声明了命名空间，标签会带前缀
    if root.tag.endswith("opml"):
        pass
    else:
        # 容忍根标签不是 opml 的情况——只要结构像就行
        if "opml" not in root.tag.lower():
            raise ValueError(f"这不是 OPML 文件（根标签是 {root.tag}）")

    result = OpmlResult()

    # 标题
    for head in root.iter():
        if head.tag.endswith("head"):
            for child in head:
                if child.tag.endswith("title"):
                    result.title = _clean_text(child.text)
                    break
            break

    # 找 body
    body = None
    for node in root:
        if node.tag.endswith("body"):
            body = node
            break
    if body is None:
        raise ValueError("OPML 文件缺少 body 节点")

    _walk(body, None, result, depth=0)
    return result


def _walk(node, category: str | None, result: OpmlResult, depth: int) -> None:
    """递归遍历 outline 树。"""
    if depth > MAX_DEPTH:
        return

    for outline in node:
        if not outline.tag.endswith("outline"):
            continue

        if len(result.entries) + len(result.skipped) >= MAX_OUTLINES:
            logger.warning("OPML 条目超过上限 %d，停止解析", MAX_OUTLINES)
            return

        result.total_outlines += 1
        attrs = outline.attrib

        text = _clean_text(attrs.get("text")) or _clean_text(attrs.get("title"))
        # xmlUrl 是 feed 地址，htmlUrl 才是站点主页
        xml_url = attrs.get("xmlUrl") or attrs.get("xmlurl")
        html_url = attrs.get("htmlUrl") or attrs.get("htmlurl")

        child_categories = [
            child for child in outline if child.tag.endswith("outline")
        ]

        # 有子节点且没有 xmlUrl/htmlUrl → 这是个分类分组，不是订阅项
        if child_categories and not (xml_url or html_url):
            group_name = text or f"分类{depth + 1}"
            for child in child_categories:
                _walk([child], group_name, result, depth + 1)
            continue

        if not (xml_url or html_url):
            # 只有一个 text 的空节点，跳过但记录
            if text:
                result.skipped.append((text, "缺少网址"))
            continue

        # ---- 决定站点 URL 与 feed URL ----
        if html_url:
            site_url = _normalize_site_url(html_url)
            inferred = False
        elif xml_url:
            site_url = _normalize_site_url(_infer_site_from_feed(xml_url) or "")
            inferred = True
        else:
            site_url = None

        if not site_url:
            result.skipped.append((text or xml_url or "?", "网址格式无效"))
            continue

        feed_url = normalize_url(xml_url) if xml_url else None

        # 有 htmlUrl 时 xmlUrl 可能指向同一个 host 的 feed，这是正常情况
        if feed_url and feed_url == site_url:
            feed_url = None

        if not text:
            # 没有标题就用主机名，导入后用户可以改名
            text = urlsplit(site_url).netloc or site_url

        result.entries.append(
            OpmlEntry(
                name=text[:256],
                site_url=site_url,
                feed_url=feed_url,
                category=category,
                inferred_site=bool(xml_url and not html_url),
            )
        )

        # 子节点仍要遍历——某些导出器会在订阅项下嵌套子项
        for child in child_categories:
            _walk([child], category or text, result, depth + 1)


def dedupe_entries(entries: list[OpmlEntry]) -> tuple[list[OpmlEntry], int]:
    """按域名去重。返回 (去重后的列表, 被去掉的重复数)。

    OPML 里同一个站点常出现多次——用户可能订阅了多个 feed
    （全文 + 分类），也可能手工重复添加过。站点层面只保留一条，
    但要记住见过哪些 feed 地址，后续抓取时可以合并使用。
    """
    seen: dict[str, OpmlEntry] = {}
    dropped = 0

    for entry in entries:
        key = site_hash(entry.site_url)
        existing = seen.get(key)

        if existing is None:
            seen[key] = entry
            continue

        dropped += 1
        # 合并：已有一条但缺 feed 地址，而新来的有 → 补上
        if not existing.feed_url and entry.feed_url:
            existing.feed_url = entry.feed_url
        # htmlUrl 明确的比推断的更可靠
        if existing.inferred_site and not entry.inferred_site:
            existing.inferred_site = False
            existing.site_url = entry.site_url

    return list(seen.values()), dropped