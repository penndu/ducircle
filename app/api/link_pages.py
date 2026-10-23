"""友链页管理接口。

用户填一个友链页地址，系统解析出上面的所有友情链接并加入列表，
之后定期重新检测，发现新友链自动补进来。
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select

from app.api.deps import CurrentUserSession, DbSession
from app.core.security import normalize_url, url_hash
from app.models import LinkPage
from app.services.link_page_service import LinkPageError, scan_link_page

router = APIRouter(prefix="/api/link-pages", tags=["友链页"])


class LinkPageCreate(BaseModel):
    url: str = Field(min_length=4, max_length=1024)
    name: str | None = Field(default=None, max_length=256)
    note: str | None = Field(default=None, max_length=256)
    # 是否立即扫描。默认 True——用户加了就该看到结果。
    scan_now: bool = True

    @field_validator("url")
    @classmethod
    def check_url(cls, v: str) -> str:
        v = v.strip()
        if not v.startswith(("http://", "https://")):
            v = "https://" + v
        normalized = normalize_url(v)
        if not normalized.startswith(("http://", "https://")):
            raise ValueError("不是有效的网址")
        return v


class LinkPageOut(BaseModel):
    id: int
    url: str
    name: str | None = None
    note: str | None = None
    found_count: int = 0
    added_count: int = 0
    last_checked_at: datetime | None = None
    last_error: str | None = None
    active: bool = True
    created_at: datetime

    # 从 ORM 对象取值——少了这行 model_validate 会报
    # "Input should be a valid dictionary or instance of LinkPageOut"
    model_config = ConfigDict(from_attributes=True)


class LinkPageScanResult(BaseModel):
    link_page_id: int
    total: int = 0
    added: int = 0
    skipped: int = 0
    blocked: int = 0
    error: str | None = None
    # 新发现的友链预览
    new_sites: list[dict] = Field(default_factory=list)


@router.get("", response_model=list[LinkPageOut], summary="友链页列表")
async def list_link_pages(user: CurrentUserSession, db: DbSession) -> list[LinkPageOut]:
    rows = (
        await db.execute(
            select(LinkPage)
            .where(LinkPage.user_id == user.id)
            .order_by(LinkPage.created_at.desc())
        )
    ).scalars().all()
    return [LinkPageOut.model_validate(r) for r in rows]


@router.post(
    "", response_model=LinkPageOut, status_code=status.HTTP_201_CREATED, summary="添加友链页"
)
async def create_link_page(
    payload: LinkPageCreate, user: CurrentUserSession, db: DbSession
) -> LinkPageOut:
    url = payload.url
    u_hash = url_hash(url)

    existing = (
        await db.execute(
            select(LinkPage).where(
                LinkPage.user_id == user.id, LinkPage.url_hash == u_hash
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(status_code=409, detail="已经添加过这个友链页了")

    page = LinkPage(
        user_id=user.id,
        url=url,
        url_hash=u_hash,
        name=payload.name,
        note=payload.note,
    )
    db.add(page)
    await db.flush()

    if payload.scan_now:
        result = await scan_link_page(db, user.id, page)
        if result.error and result.total == 0:
            # 解析失败不留垃圾数据，让用户改地址
            await db.delete(page)
            await db.commit()
            raise HTTPException(status_code=400, detail=result.error)

    await db.commit()
    return LinkPageOut.model_validate(page)


@router.post(
    "/{page_id}/scan",
    response_model=LinkPageScanResult,
    summary="立即重新检测友链页",
)
async def scan_link_page_now(
    page_id: int, user: CurrentUserSession, db: DbSession
) -> LinkPageScanResult:
    page = (
        await db.execute(
            select(LinkPage).where(
                LinkPage.id == page_id, LinkPage.user_id == user.id
            )
        )
    ).scalar_one_or_none()
    if page is None:
        raise HTTPException(status_code=404, detail="友链页不存在")

    # 先记下扫描前就有的友链——scan 之后新的已经入库了，
    # 不先记就没法区分「本次新增的」和「早就有的」。
    from app.core.security import site_hash
    from app.models import Site

    before = (
        await db.execute(select(Site).where(Site.user_id == user.id))
    ).scalars().all()
    known_before = {s.url_hash for s in before}

    result = await scan_link_page(db, user.id, page)

    # 把本次新增的友链列出来给用户看
    new_sites = []
    for entry in result.entries:
        if site_hash(entry.url) not in known_before:
            new_sites.append({"name": entry.name, "url": entry.url})

    return LinkPageScanResult(
        link_page_id=result.link_page_id,
        total=result.total,
        added=result.added,
        skipped=result.skipped,
        blocked=result.blocked,
        error=result.error,
        new_sites=new_sites[:50],
    )


@router.delete("/{page_id}", summary="删除友链页")
async def delete_link_page(
    page_id: int, user: CurrentUserSession, db: DbSession
) -> dict:
    page = (
        await db.execute(
            select(LinkPage).where(
                LinkPage.id == page_id, LinkPage.user_id == user.id
            )
        )
    ).scalar_one_or_none()
    if page is None:
        raise HTTPException(status_code=404, detail="友链页不存在")

    await db.delete(page)
    await db.commit()
    # 已经加进来的友链不删——它们已经是独立的友链了，
    # 是否保留该由用户自己在友链列表里决定。
    return {"ok": True}


@router.post("/{page_id}/toggle", summary="启用/停用定期检测")
async def toggle_link_page(
    page_id: int, user: CurrentUserSession, db: DbSession
) -> dict:
    page = (
        await db.execute(
            select(LinkPage).where(
                LinkPage.id == page_id, LinkPage.user_id == user.id
            )
        )
    ).scalar_one_or_none()
    if page is None:
        raise HTTPException(status_code=404, detail="友链页不存在")

    page.active = not page.active
    await db.commit()
    return {"ok": True, "active": page.active}
