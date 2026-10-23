"""数据导出接口。"""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Query
from fastapi.responses import StreamingResponse

from app.api.deps import CurrentUserSession, DbSession
from app.services.exporter import (
    export_all,
    filename_for,
    stream_export,
    to_csv,
    to_json,
    to_markdown,
)

router = APIRouter(prefix="/api/export", tags=["数据导出"])


@router.get("", summary="导出我的数据")
async def export_data(
    user: CurrentUserSession,
    db: DbSession,
    format: Annotated[
        Literal["json", "csv", "markdown"], Query(description="导出格式")
    ] = "json",
    stream: Annotated[bool, Query(description="大数据集建议开启流式")] = False,
):
    """导出全部友链与文章。

    数据完全属于用户，随时可以带走。
    默认一次性返回（便于下载），数据集很大时用 stream=true 分批输出。
    """
    filename = filename_for(format)

    if stream:
        return StreamingResponse(
            stream_export(db, format),
            media_type=_mime(format),
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"'
            },
        )

    data = await export_all(db)

    if format == "csv":
        content = to_csv(data)
    elif format == "markdown":
        content = to_markdown(data)
    else:
        content = to_json(data)

    # 中文文件名需要 RFC 5987 编码，直接放 UTF-8 会让部分浏览器乱码
    from urllib.parse import quote

    encoded = quote(filename)

    return StreamingResponse(
        iter([content]),
        media_type=_mime(format),
        headers={
            "Content-Disposition": (
                f"attachment; filename=\"{filename}\"; "
                f"filename*=UTF-8''{encoded}"
            )
        },
    )


def _mime(fmt: str) -> str:
    return {
        "json": "application/json; charset=utf-8",
        "csv": "text/csv; charset=utf-8",
        "markdown": "text/markdown; charset=utf-8",
    }.get(fmt, "application/octet-stream")