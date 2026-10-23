"""数据导出。

三种格式对应三种用途：
- JSON：程序可读，完整保留字段（含摘要），用于备份或二次处理
- CSV：表格工具可读，用于自己分析
- Markdown：直接贴到博客里，人可读

用户数据完全属于自己，导出必须是完整且无损的。
"""

from __future__ import annotations

import csv
import io
import json
from datetime import datetime
from typing import AsyncIterator

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Post, Site


async def export_all(db: AsyncSession) -> dict:
    """导出全部数据（内存版，用于小数据集）。"""
    sites = (await db.execute(select(Site).order_by(Site.name))).scalars().all()
    site_map = {s.id: s for s in sites}

    posts = (
        await db.execute(
            select(Post).where(Post.deleted_at.is_(None)).order_by(Post.created.desc())
        )
    ).scalars().all()

    return {
        "exported_at": datetime.now().isoformat(),
        "version": 1,
        "sites": [
            {
                "name": s.name,
                "url": s.url,
                "feed_url": s.feed_url,
                "avatar": s.avatar,
                "description": s.description,
                "note": s.note,
                "active": s.active,
                "error": s.error,
                "strategy": s.strategy,
                "framework": s.framework,
                "last_success_at": (
                    s.last_success_at.isoformat() if s.last_success_at else None
                ),
                "post_count": sum(1 for p in posts if p.site_id == s.id),
            }
            for s in sites
        ],
        "posts": [
            {
                "title": p.title,
                "link": p.link,
                "site": site_map[p.site_id].name if p.site_id in site_map else "",
                "site_url": site_map[p.site_id].url if p.site_id in site_map else "",
                "created": p.created.isoformat() if p.created else None,
                "updated": p.updated.isoformat() if p.updated else None,
                "summary": p.summary,
                "score": p.ai_score,
                "strategy": p.strategy,
            }
            for p in posts
        ],
    }


def to_json(data: dict) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2)


def to_csv(data: dict) -> str:
    """站点和文章分开两张表——它们的主键不同，混在一张表里很别扭。"""
    buffer = io.StringIO()
    writer = csv.writer(buffer)

    writer.writerow(["# 友链站点"])
    writer.writerow(["名称", "网址", "RSS", "简介", "备注", "状态", "策略", "文章数"])
    for site in data["sites"]:
        writer.writerow([
            site["name"],
            site["url"],
            site.get("feed_url") or "",
            site.get("description") or "",
            site.get("note") or "",
            "正常" if not site.get("error") else "异常",
            site.get("strategy") or "",
            site.get("post_count", 0),
        ])

    writer.writerow([])
    writer.writerow(["# 文章"])
    writer.writerow(["标题", "链接", "来源", "发布时间", "推荐分", "摘要"])
    for post in data["posts"]:
        writer.writerow([
            post["title"],
            post["link"],
            post.get("site", ""),
            post.get("created") or "",
            post.get("score") if post.get("score") is not None else "",
            post.get("summary") or "",
        ])

    # Excel 打开 CSV 需要 BOM 才能正确识别 UTF-8
    return "﻿" + buffer.getvalue()


def to_markdown(data: dict, max_posts: int = 200) -> str:
    """导出为 Markdown。只保留最近的文章，全量导出会产生巨大的文件。"""
    lines = [
        f"# 友链朋友圈导出",
        "",
        f"导出时间：{data['exported_at']}",
        f"友链数：{len(data['sites'])}　文章数：{len(data['posts'])}",
        "",
        "## 友链列表",
        "",
    ]

    for site in data["sites"]:
        mark = "（异常）" if site.get("error") else ""
        lines.append(f"- [{site['name']}]({site['url']}){mark}")

    lines.extend(["", "## 最新文章", ""])

    posts = data["posts"][:max_posts]
    for post in posts:
        date = (post.get("created") or "")[:10]
        lines.append(f"### [{post['title']}]({post['link']})")
        lines.append("")
        meta = f"`{date}`　来自 [{post.get('site', '')}]({post.get('site_url', '')})"
        if post.get("score") is not None:
            meta += f"　推荐分 {post['score']:.1f}"
        lines.append(meta)
        lines.append("")
        if post.get("summary"):
            lines.append(post["summary"])
            lines.append("")

    if len(data["posts"]) > max_posts:
        lines.append(f"\n> 仅显示最近 {max_posts} 篇，共 {len(data['posts'])} 篇。")

    return "\n".join(lines)


def filename_for(fmt: str) -> str:
    stamp = datetime.now().strftime("%Y%m%d")
    ext = {"json": "json", "csv": "csv", "markdown": "md"}.get(fmt, "txt")
    return f"ducircle-export-{stamp}.{ext}"


async def stream_export(db: AsyncSession, fmt: str) -> AsyncIterator[str]:
    """流式导出，避免大数据集一次性占满内存。

    按 500 条一批处理，批次之间让出事件循环。
    """
    batch_size = 500
    offset = 0

    if fmt == "json":
        yield '{"exported_at": "' + datetime.now().isoformat() + '", "sites": ['
        first = True
        while True:
            sites = (
                await db.execute(
                    select(Site).order_by(Site.id).limit(batch_size).offset(offset)
                )
            ).scalars().all()
            if not sites:
                break
            chunk = [
                {
                    "name": s.name, "url": s.url, "avatar": s.avatar,
                    "active": s.active, "error": s.error,
                }
                for s in sites
            ]
            text = json.dumps(chunk, ensure_ascii=False)
            # 去掉数组括号，拼成流式输出
            text = text[1:-1]
            yield ("" if first else ", ") + text
            first = False
            offset += batch_size

        yield '], "posts": ['
        offset = 0
        first = True
        while True:
            posts = (
                await db.execute(
                    select(Post)
                    .where(Post.deleted_at.is_(None))
                    .order_by(Post.id)
                    .limit(batch_size)
                    .offset(offset)
                )
            ).scalars().all()
            if not posts:
                break
            chunk = [
                {"title": p.title, "link": p.link, "summary": p.summary}
                for p in posts
            ]
            text = json.dumps(chunk, ensure_ascii=False)[1:-1]
            yield ("" if first else ", ") + text
            first = False
            offset += batch_size
        yield "]}"

    else:
        data = await export_all(db)
        if fmt == "csv":
            yield to_csv(data)
        else:
            yield to_markdown(data)