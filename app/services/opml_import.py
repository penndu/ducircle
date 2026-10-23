"""OPML 导入服务。

把解析出的条目写入数据库，处理与现有友链的冲突。

导入策略：
- 同域名已存在 → 跳过（不覆盖用户改过的名称和备注）
- 已在黑名单 → 跳过
- 新条目 → 插入，source 标记为 opml 并记录发现来源
- 只导入 feed 地址、没�� htmlUrl 的条目 → 标记为待确认
"""

from __future__ import annotations

import logging
import re

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.core.security import site_hash
from app.models import Blacklist, Site
from app.services.opml_parser import OpmlEntry, dedupe_entries, parse_opml

logger = logging.getLogger("ducircle.import")


async def import_opml(
    content: str | bytes,
    user_id: int,
    db,
    *,
    include_inferred: bool = True,
    max_sites: int = 500,
) -> dict:
    """导入 OPML。返回详细的导入结果供用户核对。

    max_sites 有上限——用户可能导入几千条的订阅列表，
    一次性全插会让首屏卡住，也超出了几百人规模的设计假设。
    """
    parsed = parse_opml(content)

    # 黑名单
    blacklist = (await db.execute(select(Blacklist))).scalars().all()
    blacklist_patterns = [(b.pattern, b.is_regex) for b in blacklist]

    entries, duplicates = dedupe_entries(parsed.entries)
    entries = entries[:max_sites]

    # 已存在的域名
    existing_hashes = {
        h for h in (await db.execute(select(Site.url_hash))).scalars().all()
    }

    created = 0
    skipped_existing = 0
    skipped_blacklist: list[str] = []
    skipped_limit = 0
    pending_review: list[str] = []

    for entry in entries:
        url_hash = site_hash(entry.site_url)

        # 推断出来的站点 URL 风险较高，默认也导入但标记出来让用户核对
        if entry.inferred_site and not include_inferred:
            skipped_limit += 1
            continue

        if _is_blacklisted(entry.site_url, blacklist_patterns):
            skipped_blacklist.append(entry.name)
            continue

        if url_hash in existing_hashes:
            skipped_existing += 1
            continue

        site = Site(
            user_id=user_id,
            name=entry.name,
            url=entry.site_url,
            url_hash=url_hash,
            feed_url=entry.feed_url,
            source="opml",
            note=(
                f"OPML 导入· {entry.category}" if entry.category
                else "OPML 导入"
            ),
        )
        db.add(site)
        existing_hashes.add(url_hash)
        created += 1

        if entry.inferred_site:
            pending_review.append(entry.name)

    try:
        await db.flush()
    except IntegrityError as exc:
        # 并发导入同一份 OPML 时靠唯一索引兜底
        await db.rollback()
        logger.warning("OPML 导入冲突: %s", exc)
        raise ValueError("导入时发生冲突，可能是在重复导入同一份文件") from exc

    result = {
        "ok": True,
        "title": parsed.title,
        "total_outlines": parsed.total_outlines,
        "parsed_entries": len(parsed.entries),
        "duplicates_in_file": duplicates,
        "created": created,
        "skipped_existing": skipped_existing,
        "skipped_blacklist": len(skipped_blacklist),
        "blacklist_names": skipped_blacklist[:20],
        "skipped_limit": max(
            0,
            len(parsed.entries) - len(entries) - duplicates,
        ),
        "limit": max_sites,
        "pending_review": pending_review[:20],
        "pending_review_count": len(pending_review),
        "invalid": parsed.skipped[:20],
        "invalid_count": len(parsed.skipped),
    }

    logger.info(
        "OPML 导入完成：解析 %d 条，新建 %d，跳过已存在 %d，黑名单 %d",
        len(parsed.entries), created, skipped_existing, len(skipped_blacklist),
    )
    return result


def _is_blacklisted(url: str, patterns: list[tuple[str, bool]]) -> bool:
    """检查是否命中黑名单。与sites.py 的实现保持一致。"""
    for pattern, is_regex in patterns:
        if is_regex:
            try:
                if re.search(pattern, url):
                    return True
            except re.error:
                continue
        elif pattern in url:
            return True
    return False


def generate_sample_opml() -> str:
    """生成示例 OPML，用于帮助用户理解格式。"""
    return """<?xml version="1.0" encoding="UTF-8"?>
<opml version="2.0">
  <head>
    <title>我的订阅</title>
  </head>
  <body>
    <outline type="rss" text="示例博客A"
             htmlUrl="https://example-a.com/"
             xmlUrl="https://example-a.com/atom.xml"/>
    <outline type="rss" text="示例博客B"
             htmlUrl="https://example-b.com/"
             xmlUrl="https://example-b.com/feed.xml"/>
    <outline text="技术分类">
      <outline type="rss" text="示例博客C"
               htmlUrl="https://example-c.com/"
               xmlUrl="https://example-c.com/rss.xml"/>
    </outline>
  </body>
</opml>
"""


# 类型别名方便调用方标注
EntryList = list[OpmlEntry]