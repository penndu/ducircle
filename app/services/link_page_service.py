"""友链页服务：解析友链页、批量入库、定期检测新友链。

流程：
    1. 用户填一个友链页地址（https://example.com/friends/）
    2. 抓取该页，解析出上面所有友情链接
    3. 与已有友链去重（按 url_hash），新增的写进 sites
       source=link_page，discovered_from=友链页地址
    4. 之后定期重跑第 2、3 步，发现新友链自动补进来

去重有两层：
    - 同一次采集内部：按域名去重（一个站点只留一条）
    - 与已有数据：按 sites.url_hash 去重（该用户已加过就跳过）
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from urllib.parse import urljoin, urlparse

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.security import normalize_url, site_hash, url_hash
from app.models import Blacklist, LinkPage, Site, SiteSource
from app.parser.link_page import LinkEntry, parse_link_page

logger = logging.getLogger("ducircle.linkpage")


class LinkPageError(Exception):
    """友链页处理失败。"""


@dataclass
class ScanResult:
    """一次友链页扫描的结果。"""

    link_page_id: int
    total: int = 0
    added: int = 0
    skipped: int = 0
    blocked: int = 0
    entries: list[LinkEntry] = field(default_factory=list)
    error: str | None = None


async def fetch_link_page_html(
    url: str,
    settings: Settings | None = None,
    client: httpx.AsyncClient | None = None,
) -> str:
    """抓取友链页 HTML。"""
    settings = settings or get_settings()

    own_client = client is None
    if own_client:
        client = httpx.AsyncClient(
            timeout=httpx.Timeout(settings.crawl_timeout),
            headers={
                "User-Agent": settings.user_agent,
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            },
            follow_redirects=True,
        )

    try:
        response = await client.get(url)
        response.raise_for_status()
        # 编码：不少友链页没写 charset，靠 httpx 猜容易猜错
        content = response.text
        if not response.encoding or response.encoding.lower() == "iso-8859-1":
            content = response.content.decode("utf-8", errors="replace")
        return content
    except httpx.HTTPStatusError as exc:
        raise LinkPageError(
            f"HTTP {exc.response.status_code}：{exc.response.reason_phrase}"
        ) from exc
    except httpx.HTTPError as exc:
        raise LinkPageError(f"网络请求失败：{exc}") from exc
    finally:
        if own_client:
            await client.aclose()


def _matches_blacklist(pattern: str, is_regex: bool, url: str) -> bool:
    """检查 URL 是否命中黑名单。"""
    import re

    if is_regex:
        try:
            return re.search(pattern, url) is not None
        except re.error:
            return False
    return pattern.lower() in url.lower()


async def _is_blocked(db: AsyncSession, user_id: int, url: str) -> bool:
    """检查 URL 是否在用户黑名单里。"""
    rows = (
        await db.execute(select(Blacklist).where(Blacklist.user_id == user_id))
    ).scalars().all()
    for item in rows:
        if _matches_blacklist(item.pattern, item.is_regex, url):
            return True
    return False


async def scan_link_page(
    db: AsyncSession,
    user_id: int,
    link_page: LinkPage,
    settings: Settings | None = None,
    client: httpx.AsyncClient | None = None,
    *,
    dry_run: bool = False,
) -> ScanResult:
    """扫描一个友链页，把发现的新友链加入列表。

    dry_run=True 时只统计不写库——用于预览「会发现哪些新友链」。
    """
    settings = settings or get_settings()
    result = ScanResult(link_page_id=link_page.id)

    try:
        html = await fetch_link_page_html(link_page.url, settings, client)
    except LinkPageError as exc:
        result.error = str(exc)
        if not dry_run:
            link_page.last_error = str(exc)
            link_page.last_checked_at = datetime.utcnow()
            await db.commit()
        return result

    entries = parse_link_page(html, link_page.url)
    result.entries = entries
    result.total = len(entries)

    if not entries:
        result.error = "页面里没有解析出友链（可能是反爬、页面结构特殊或需要登录）"
        if not dry_run:
            link_page.last_error = result.error
            link_page.last_checked_at = datetime.utcnow()
            await db.commit()
        return result

    # 已有的友链（该用户的），按 url_hash 索引
    existing = (
        await db.execute(select(Site).where(Site.user_id == user_id))
    ).scalars().all()
    existing_hashes = {s.url_hash for s in existing}

    to_add: list[LinkEntry] = []
    for entry in entries:
        site_h = site_hash(entry.url)
        if site_h in existing_hashes:
            result.skipped += 1
            continue
        # 黑名单
        if await _is_blocked(db, user_id, entry.url):
            result.blocked += 1
            continue
        to_add.append(entry)

    result.added = len(to_add)

    if dry_run:
        return result

    now = datetime.utcnow()
    for entry in to_add:
        site_h = site_hash(entry.url)
        db.add(
            Site(
                user_id=user_id,
                name=(entry.name or urlparse(entry.url).netloc)[:256],
                url=entry.url,
                url_hash=site_h,
                avatar=entry.avatar,
                description=entry.description,
                source=SiteSource.LINK_PAGE.value,
                discovered_from=link_page.url,
            )
        )
        existing_hashes.add(site_h)

    await db.flush()

    link_page.found_count = result.total
    link_page.added_count = result.added
    link_page.last_checked_at = now
    link_page.last_error = None
    if not link_page.name:
        link_page.name = _extract_page_title(html) or link_page.url
    await db.commit()

    logger.info(
        "友链页 %s 发现 %d 条，新增 %d 条，跳过 %d 条",
        link_page.url, result.total, result.added, result.skipped,
    )
    return result


def _extract_page_title(html: str) -> str | None:
    """从 HTML 里取 <title>。"""
    import re

    match = re.search(r"<title[^>]*>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
    if not match:
        return None
    title = match.group(1).strip()
    # 去掉 " - 站点名" 这类后缀
    title = re.split(r"\s*[-–—|]\s*", title)[0].strip()
    return title[:256] or None


async def scan_all_link_pages(
    db: AsyncSession,
    settings: Settings | None = None,
    client: httpx.AsyncClient | None = None,
) -> list[ScanResult]:
    """扫描所有用户的全部友链页。

    调度器定期调用。逐个用户处理——租户隔离靠显式的
    user_id 过滤，而不是 ORM 钩子（这里查的是 LinkPage，
    钩子只覆盖租户业务表）。
    """
    settings = settings or get_settings()

    pages = (await db.execute(select(LinkPage).where(LinkPage.active.is_(True)))).scalars().all()
    results: list[ScanResult] = []

    for page in pages:
        try:
            result = await scan_link_page(db, page.user_id, page, settings, client)
            results.append(result)
        except Exception as exc:  # noqa: BLE001
            # 一个友链页失败不能影响其它
            logger.exception("友链页 %s 扫描失败", page.url)
            results.append(
                ScanResult(link_page_id=page.id, error=str(exc))
            )
            await db.rollback()

    return results
