"""回填 posts.raw_content——从各站点的 RSS feed 里取全文。

背景：很多站点的文章页是 SPA 骨架页（正文靠 JS 渲染），
静态抓取拿不到正文，摘要生成 90% 失败。但这些站的 feed 里带着全文。

跑法（容器内）：
    python scripts/backfill_raw_content.py

幂等：已有 raw_content 的不重复填。
"""

from __future__ import annotations

import asyncio
import logging
import sys

import httpx

from app.core.config import get_settings
from app.db import session_scope
from app.models import Post, Site
from app.parser.feed import parse_feed
from sqlalchemy import select, update

logger = logging.getLogger("ducircle.backfill")

# 站点没有全文时不必再试
MAX_ATTEMPTS_PER_SITE = 1


async def fetch_feed_text(client: httpx.AsyncClient, feed_url: str) -> dict[str, str]:
    """抓 feed，返回 {链接: 全文HTML}。"""
    resp = await client.get(feed_url)
    resp.raise_for_status()
    site = parse_feed(resp.content, feed_url, feed_url)
    if site is None:
        return {}
    out: dict[str, str] = {}
    for post in site.posts:
        if post.content:
            out[post.link] = post.content
    return out


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    settings = get_settings()

    async with session_scope() as db:
        sites = (
            await db.execute(
                select(Site).where(
                    Site.deleted_at.is_(None), Site.active.is_(True)
                )
            )
        ).scalars().all()

        total_filled = 0
        total_sites = 0

        async with httpx.AsyncClient(
            timeout=30,
            follow_redirects=True,
            headers={
                "User-Agent": settings.user_agent,
                "Accept-Language": "zh-CN,zh;q=0.9",
            },
        ) as client:
            for site in sites:
                # 找出这个站还缺全文的文章
                pending = (
                    await db.execute(
                        select(Post).where(
                            Post.site_id == site.id,
                            Post.deleted_at.is_(None),
                            Post.raw_content.is_(None),
                        )
                    )
                ).scalars().all()

                if not pending:
                    continue
                total_sites += 1

                # 找 feed 地址：站点配置优先，否则按常见路径试
                candidates = []
                if site.feed_url:
                    candidates.append(site.feed_url)
                else:
                    from urllib.parse import urljoin

                    base = site.url.rstrip("/")
                    candidates = [
                        urljoin(base + "/", p)
                        for p in ("feed/", "feed", "rss/", "atom.xml")
                    ]

                contents: dict[str, str] = {}
                for feed_url in candidates:
                    try:
                        contents = await fetch_feed_text(client, feed_url)
                        if contents:
                            logger.info(
                                "站点 %s（%s）feed %s 取到 %d 篇全文",
                                site.id, site.name, feed_url, len(contents),
                            )
                            break
                    except Exception as exc:  # noqa: BLE001
                        logger.debug("feed %s 取全文失败：%s", feed_url, exc)

                if not contents:
                    logger.info("站点 %s（%s）没取到 feed 全文", site.id, site.name)
                    continue

                # 按标题+链接匹配回填
                filled = 0
                for post in pending:
                    raw = contents.get(post.link)
                    if not raw:
                        # 链接对不上时按标题找（永久链接可能变了）
                        for link, content in contents.items():
                            if link.rstrip("/").endswith(
                                post.link.rstrip("/").split("/")[-2]
                                if "/" in post.link.rstrip("/") else ""
                            ):
                                raw = content
                                break
                    if raw:
                        await db.execute(
                            update(Post)
                            .where(Post.id == post.id)
                            .values(raw_content=raw[:20000])
                        )
                        filled += 1

                if filled:
                    await db.commit()
                    total_filled += filled
                    logger.info(
                        "站点 %s（%s）回填 %d 篇全文", site.id, site.name, filled
                    )

        logger.info(
            "回填完成：%d 个站点，共填了 %d 篇", total_sites, total_filled
        )


if __name__ == "__main__":
    asyncio.run(main())
