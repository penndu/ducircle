"""OPML 导入接口。"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, File, Form, HTTPException, Query, UploadFile, status
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

from app.api.deps import CurrentUserSession, DbSession
from app.services.opml_import import generate_sample_opml, import_opml

router = APIRouter(prefix="/api/opml", tags=["OPML 导入"])

# 单个 OPML 文件不该很大。10MB 足够装几千条订阅了。
MAX_FILE_SIZE = 10 * 1024 * 1024


class ImportResultOut(BaseModel):
    ok: bool
    title: str
    total_outlines: int
    parsed_entries: int
    duplicates_in_file: int
    created: int
    skipped_existing: int
    skipped_blacklist: int
    skipped_limit: int
    pending_review_count: int
    invalid_count: int
    blacklist_names: list[str]
    pending_review: list[str]
    invalid: list[list[str]]
    limit: int


@router.post("/import", response_model=ImportResultOut, summary="导入 OPML 文件")
async def import_file(
    user: CurrentUserSession,
    db: DbSession,
    file: Annotated[UploadFile, File(description="OPML 文件")],
    include_inferred: Annotated[
        bool,
        Form(description="是否导入仅从 feed 地址推断出站点 URL 的条目"),
    ] = True,
    max_sites: Annotated[int, Form(ge=1, le=2000)] = 500,
) -> ImportResultOut:
    """上传 OPML 订阅列表，批量导入友链。

    从阅读器（FreshRSS / TinyRSS / Feedly / NetNewsWire 等）导出即可。
    """
    filename = (file.filename or "").lower()
    if not filename.endswith(".opml") and not filename.endswith(".xml"):
        raise HTTPException(
            status_code=400, detail="请上传 .opml 文件（阅读器导出的订阅列表）"
        )

    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="文件内容为空")
    if len(content) > MAX_FILE_SIZE:
        raise HTTPException(
            status_code=413, detail="文件太大（上限 10MB）"
        )

    try:
        result = await import_opml(
            content, user.id, db,
            include_inferred=include_inferred,
            max_sites=max_sites,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return ImportResultOut(**result)


@router.post(
    "/import-text",
    response_model=ImportResultOut,
    summary="直接粘贴 OPML 内容导入",
)
async def import_text(
    user: CurrentUserSession,
    db: DbSession,
    content: Annotated[str, Form()],
    include_inferred: Annotated[bool, Form()] = True,
) -> ImportResultOut:
    """粘贴文本导入。适合从网页上复制的 OPML 内容。"""
    if not content.strip():
        raise HTTPException(status_code=400, detail="内容为空")
    if len(content) > MAX_FILE_SIZE:
        raise HTTPException(status_code=413, detail="内容太大")

    try:
        result = await import_opml(content, user.id, db, include_inferred=include_inferred)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return ImportResultOut(**result)


@router.get(
    "/sample",
    response_class=PlainTextResponse,
    summary="下载示例 OPML",
)
async def sample() -> PlainTextResponse:
    """返回示例文件。用户在阅读器里照这个格式导出即可。"""
    return PlainTextResponse(
        generate_sample_opml(),
        media_type="text/x-opml",
        headers={
            "Content-Disposition": 'attachment; filename="ducircle-sample.opml"'
        },
    )


@router.post(
    "/preview",
    summary="只解析不导入（预览）",
)
async def preview(
    user: CurrentUserSession,
    db: DbSession,
    file: Annotated[UploadFile, File()],
) -> dict:
    """解析但不写入。

    导入前先看看会新增哪些、会跳过哪些，比直接导入再回滚友好得多。
    """
    from app.services.opml_parser import dedupe_entries, parse_opml

    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="文件内容为空")
    if len(content) > MAX_FILE_SIZE:
        raise HTTPException(status_code=413, detail="文件太大")

    try:
        parsed = parse_opml(content)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    entries, duplicates = dedupe_entries(parsed.entries)

    from sqlalchemy import select

    from app.core.security import site_hash
    from app.models import Site

    existing = {
        h for h in (await db.execute(select(Site.url_hash))).scalars().all()
    }

    to_create = [e for e in entries if site_hash(e.site_url) not in existing]
    to_skip = [e for e in entries if site_hash(e.site_url) in existing]

    return {
        "title": parsed.title,
        "total_outlines": parsed.total_outlines,
        "parsed_entries": len(parsed.entries),
        "duplicates_in_file": duplicates,
        "will_create": len(to_create),
        "will_skip_existing": len(to_skip),
        "invalid_count": len(parsed.skipped),
        "preview": [
            {
                "name": e.name,
                "site_url": e.site_url,
                "feed_url": e.feed_url,
                "category": e.category,
                "inferred_site": e.inferred_site,
            }
            for e in to_create[:50]
        ],
        "skipped": [{"name": e.name, "url": e.site_url} for e in to_skip[:20]],
    }