"""友链管理接口。"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError

from app.api.deps import CurrentUserSession, DbSession
from app.core.security import normalize_url, site_hash
from app.models import Blacklist, Site, SiteRule, Tag

router = APIRouter(prefix="/api/sites", tags=["友链管理"])


class SiteCreate(BaseModel):
    name: str = Field(min_length=1, max_length=256)
    url: str = Field(min_length=4, max_length=1024)
    feed_url: str | None = None
    avatar: str | None = None
    note: str | None = None
    source: Literal["manual", "link_page", "opml"] = "manual"

    @field_validator("url")
    @classmethod
    def check_url(cls, v: str) -> str:
        v = v.strip()
        if not v.startswith(("http://", "https://")):
            v = "https://" + v
        normalized = normalize_url(v)
        if not normalized:
            raise ValueError("URL 格式无效")
        return normalized


class SiteUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=256)
    url: str | None = Field(default=None, max_length=1024)
    feed_url: str | None = None
    avatar: str | None = None
    note: str | None = None
    active: bool | None = None
    crawl_interval_hours: int | None = Field(default=None, ge=1, le=168)


class SiteOut(BaseModel):
    id: int
    name: str
    url: str
    feed_url: str | None
    avatar: str | None
    note: str | None
    source: str
    strategy: str | None
    framework: str | None
    active: bool
    error: bool
    error_message: str | None
    consecutive_failures: int
    last_crawled_at: datetime | None
    last_success_at: datetime | None
    post_count: int = 0

    model_config = ConfigDict(from_attributes=True)


class RuleCreate(BaseModel):
    field: Literal["title", "link", "created", "updated", "author", "avatar"]
    selector: str = Field(min_length=1, max_length=512)
    attr: str = Field(default="text", max_length=32)
    priority: int = Field(default=100, ge=1, le=1000)
    enabled: bool = True


class BlacklistCreate(BaseModel):
    pattern: str = Field(min_length=1, max_length=512)
    is_regex: bool = False
    note: str | None = None

    @field_validator("pattern")
    @classmethod
    def check_regex(cls, v: str, info):
        if info.data.get("is_regex"):
            try:
                re.compile(v)
            except re.error as exc:
                raise ValueError(f"正则表达式无效: {exc}") from exc
        return v


async def _post_count_map(db, site_ids: list[int]) -> dict[int, int]:
    from app.models import Post

    if not site_ids:
        return {}
    rows = await db.execute(
        select(Post.site_id, func.count(Post.id))
        .where(Post.site_id.in_(site_ids), Post.deleted_at.is_(None))
        .group_by(Post.site_id)
    )
    return {sid: count for sid, count in rows.all()}


@router.get("", response_model=list[SiteOut], summary="友链列表")
async def list_sites(
    user: CurrentUserSession,
    db: DbSession,
    search: str | None = None,
    only_error: Annotated[bool, Query(description="只看抓取失败的")] = False,
    active: bool | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[SiteOut]:
    """列出我的友链。

    不需要显式写 user_id 过滤——ORM 钩子会自动注入。
    """
    stmt = select(Site)
    if search:
        stmt = stmt.where(or_(Site.name.contains(search), Site.url.contains(search)))
    if only_error:
        stmt = stmt.where(Site.error.is_(True))
    if active is not None:
        stmt = stmt.where(Site.active.is_(active))

    stmt = stmt.order_by(Site.error.desc(), Site.name).limit(limit).offset(offset)
    sites = (await db.execute(stmt)).scalars().all()

    counts = await _post_count_map(db, [s.id for s in sites])
    result = []
    for s in sites:
        out = SiteOut.model_validate(s)
        out.post_count = counts.get(s.id, 0)
        result.append(out)
    return result


@router.post(
    "", response_model=SiteOut, status_code=status.HTTP_201_CREATED, summary="添加友链"
)
async def create_site(
    payload: SiteCreate, user: CurrentUserSession, db: DbSession
) -> SiteOut:
    if await _is_blacklisted(db, payload.url):
        raise HTTPException(status_code=400, detail="该域名在黑名单中")

    url_hash = site_hash(payload.url)
    existing = (
        await db.execute(select(Site).where(Site.url_hash == url_hash))
    ).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(
            status_code=409, detail=f"已存在相同域名的友链：{existing.name}"
        )

    site = Site(
        user_id=user.id,
        name=payload.name.strip(),
        url=payload.url,
        url_hash=url_hash,
        feed_url=payload.feed_url,
        avatar=payload.avatar,
        note=payload.note,
        source=payload.source,
    )
    db.add(site)
    try:
        await db.flush()
    except IntegrityError as exc:
        # 并发添加同一域名时靠唯一索引兜底
        raise HTTPException(status_code=409, detail="已存在相同域名的友链") from exc

    return SiteOut.model_validate(site)


@router.get("/{site_id}", response_model=SiteOut, summary="友链详情")
async def get_site(site_id: int, user: CurrentUserSession, db: DbSession) -> SiteOut:
    site = await _get_site_or_404(db, site_id)
    out = SiteOut.model_validate(site)
    out.post_count = (await _post_count_map(db, [site.id])).get(site.id, 0)
    return out


@router.patch("/{site_id}", response_model=SiteOut, summary="更新友链")
async def update_site(
    site_id: int, payload: SiteUpdate, user: CurrentUserSession, db: DbSession
) -> SiteOut:
    site = await _get_site_or_404(db, site_id)

    data = payload.model_dump(exclude_unset=True)
    if "url" in data and data["url"]:
        data["url"] = normalize_url(data["url"])
        data["url_hash"] = site_hash(data["url"])
    if "name" in data and data["name"]:
        data["name"] = data["name"].strip()

    for field, value in data.items():
        setattr(site, field, value)

    await db.flush()
    return SiteOut.model_validate(site)


@router.delete("/{site_id}", summary="删除友链")
async def delete_site(
    site_id: int,
    user: CurrentUserSession,
    db: DbSession,
    purge_posts: Annotated[bool, Query(description="同时删除该站全部文章")] = True,
) -> dict:
    """删除友链。默认连带删除其文章。

    这是合规要求——站点博主要求下架时，能一键清除该站全部数据。
    """
    from app.models import Post

    site = await _get_site_or_404(db, site_id)

    deleted_posts = 0
    if purge_posts:
        result = await db.execute(
            Post.__table__.delete().where(Post.site_id == site.id)
        )
        deleted_posts = result.rowcount or 0

    await db.delete(site)
    await db.flush()
    return {"ok": True, "deleted_posts": deleted_posts}


# ---- 自定义规则 ----


class RuleOut(BaseModel):
    id: int
    field: str
    selector: str
    attr: str
    priority: int
    enabled: bool

    model_config = ConfigDict(from_attributes=True)


@router.get("/{site_id}/rules", response_model=list[RuleOut], summary="自定义抓取规则")
async def list_rules(
    site_id: int, user: CurrentUserSession, db: DbSession
) -> list[RuleOut]:
    await _get_site_or_404(db, site_id)
    rows = (
        await db.execute(
            select(SiteRule)
            .where(SiteRule.site_id == site_id)
            .order_by(SiteRule.field, -SiteRule.priority)
        )
    ).scalars().all()
    return [RuleOut.model_validate(r) for r in rows]


@router.post(
    "/{site_id}/rules",
    response_model=RuleOut,
    status_code=status.HTTP_201_CREATED,
    summary="新增自定义规则",
)
async def create_rule(
    site_id: int, payload: RuleCreate, user: CurrentUserSession, db: DbSession
) -> RuleOut:
    """新增规则。用户配了就完全信任，不做自动降级。"""
    await _get_site_or_404(db, site_id)
    rule = SiteRule(user_id=user.id, site_id=site_id, **payload.model_dump())
    db.add(rule)
    await db.flush()
    return RuleOut.model_validate(rule)


@router.delete("/{site_id}/rules/{rule_id}", summary="删除规则")
async def delete_rule(
    site_id: int, rule_id: int, user: CurrentUserSession, db: DbSession
) -> dict:
    await _get_site_or_404(db, site_id)
    rule = (
        await db.execute(
            select(SiteRule).where(
                SiteRule.id == rule_id, SiteRule.site_id == site_id
            )
        )
    ).scalar_one_or_none()
    if rule is None:
        raise HTTPException(status_code=404, detail="规则不存在")
    await db.delete(rule)
    return {"ok": True}


# ---- 黑名单 ----


class BlacklistOut(BaseModel):
    id: int
    pattern: str
    is_regex: bool
    note: str | None

    model_config = ConfigDict(from_attributes=True)


@router.get("/blacklist/all", response_model=list[BlacklistOut], summary="黑名单列表")
async def list_blacklist(user: CurrentUserSession, db: DbSession) -> list[BlacklistOut]:
    rows = (await db.execute(select(Blacklist))).scalars().all()
    return [BlacklistOut.model_validate(r) for r in rows]


@router.post(
    "/blacklist",
    response_model=BlacklistOut,
    status_code=status.HTTP_201_CREATED,
    summary="添加黑名单",
)
async def create_blacklist(
    payload: BlacklistCreate, user: CurrentUserSession, db: DbSession
) -> BlacklistOut:
    item = Blacklist(user_id=user.id, **payload.model_dump())
    db.add(item)
    await db.flush()
    return BlacklistOut.model_validate(item)


@router.delete("/blacklist/{item_id}", summary="删除黑名单")
async def delete_blacklist(
    item_id: int, user: CurrentUserSession, db: DbSession
) -> dict:
    item = (
        await db.execute(select(Blacklist).where(Blacklist.id == item_id))
    ).scalar_one_or_none()
    if item is None:
        raise HTTPException(status_code=404, detail="黑名单项不存在")
    await db.delete(item)
    return {"ok": True}


# ---- 标签 ----


class TagCreate(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    color: str | None = None


class TagOut(BaseModel):
    id: int
    name: str
    color: str | None

    model_config = ConfigDict(from_attributes=True)


@router.get("/tags/all", response_model=list[TagOut], summary="标签列表")
async def list_tags(user: CurrentUserSession, db: DbSession) -> list[TagOut]:
    rows = (await db.execute(select(Tag).order_by(Tag.name))).scalars().all()
    return [TagOut.model_validate(t) for t in rows]


@router.post(
    "/tags",
    response_model=TagOut,
    status_code=status.HTTP_201_CREATED,
    summary="创建标签",
)
async def create_tag(
    payload: TagCreate, user: CurrentUserSession, db: DbSession
) -> TagOut:
    tag = Tag(user_id=user.id, **payload.model_dump())
    db.add(tag)
    try:
        await db.flush()
    except IntegrityError as exc:
        raise HTTPException(status_code=409, detail="同名标签已存在") from exc
    return TagOut.model_validate(tag)


# ---- 辅助 ----


async def _get_site_or_404(db, site_id: int) -> Site:
    site = (
        await db.execute(select(Site).where(Site.id == site_id))
    ).scalar_one_or_none()
    if site is None:
        raise HTTPException(status_code=404, detail="友链不存在")
    return site


async def _is_blacklisted(db, url: str) -> bool:
    """检查 URL 是否命中黑名单。非正则按子串匹配。"""
    items = (await db.execute(select(Blacklist))).scalars().all()
    for item in items:
        if item.is_regex:
            try:
                if re.search(item.pattern, url):
                    return True
            except re.error:
                continue
        elif item.pattern in url:
            return True
    return False