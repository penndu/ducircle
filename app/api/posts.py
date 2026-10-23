"""文章流接口（后台 Session）与只读 API（API Key）。

分两套：
- /api/posts/*    后台用，看自己的原始数据
- /v1/*           给博客前端调，走 API Key，返回渲染好的结构
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict
from sqlalchemy import func, select

from app.api.deps import CurrentUserKey, CurrentUserSession, DbSession
from app.models import Post, Site

web_router = APIRouter(prefix="/api/posts", tags=["文章管理"])
v1_router = APIRouter(prefix="/v1", tags=["只读 API"])


class PostOut(BaseModel):
    id: int
    title: str
    link: str
    created: datetime | None
    updated: datetime | None
    site_name: str
    site_url: str
    avatar: str | None
    strategy: str | None
    summary: str | None = None
    ai_score: float | None = None


class V1Post(BaseModel):
    """给前端用的精简结构。

    字段名沿用旧工具的习惯（title/link/created/author/avatar），
    方便已有博客前端少改一点。需求确认时选了 B（可换新格式），
    这里保留兼容只是顺手，不强求。
    """

    title: str
    link: str
    created: datetime | None
    updated: datetime | None
    author: str
    avatar: str | None
    summary: str | None = None
    score: float | None = None


class V1ListResponse(BaseModel):
    data: list[V1Post]
    total: int
    start: int
    end: int


# ---- 后台接口 ----


@web_router.get("", response_model=list[PostOut], summary="我的文章")
async def list_posts(
    user: CurrentUserSession,
    db: DbSession,
    site_id: int | None = None,
    has_summary: bool | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[PostOut]:
    stmt = (
        select(Post, Site.name, Site.url, Site.avatar)
        .join(Site, Post.site_id == Site.id)
        .where(Post.deleted_at.is_(None))
    )
    if site_id is not None:
        stmt = stmt.where(Post.site_id == site_id)
    if has_summary is True:
        stmt = stmt.where(Post.summary.is_not(None))
    elif has_summary is False:
        stmt = stmt.where(Post.summary.is_(None))

    stmt = (
        stmt.order_by(Post.created.desc().nullslast(), Post.id.desc())
        .limit(limit)
        .offset(offset)
    )
    rows = (await db.execute(stmt)).all()
    return [
        PostOut(
            id=post.id,
            title=post.title,
            link=post.link,
            created=post.created,
            updated=post.updated,
            site_name=site_name,
            site_url=site_url,
            avatar=avatar,
            strategy=post.strategy,
            summary=post.summary,
            ai_score=post.ai_score,
        )
        for post, site_name, site_url, avatar in rows
    ]


@web_router.delete("/{post_id}", summary="删除文章")
async def delete_post(
    post_id: int, user: CurrentUserSession, db: DbSession
) -> dict:
    post = (
        await db.execute(select(Post).where(Post.id == post_id))
    ).scalar_one_or_none()
    if post is None:
        raise HTTPException(status_code=404, detail="文章不存在")
    await db.delete(post)
    return {"ok": True}


# ---- 只读 API ----


@v1_router.get("/all", response_model=V1ListResponse, summary="文章流")
async def v1_all(
    user: CurrentUserKey,
    db: DbSession,
    start: Annotated[int, Query(ge=0, description="起始位置")] = 0,
    end: Annotated[int, Query(ge=-1, description="结束位置，-1 或不传表示到底")] = -1,
    rule: Literal["created", "updated", "score"] = "created",
    site_id: int | None = None,
    only_summarized: Annotated[
        bool, Query(description="只看已生成摘要的文章")
    ] = False,
) -> V1ListResponse:
    """文章流。start/end 是「条数偏移」而非页码，与旧工具一致。"""
    stmt = (
        select(Post, Site.name, Site.avatar)
        .join(Site, Post.site_id == Site.id)
        .where(Post.deleted_at.is_(None))
    )
    if site_id is not None:
        stmt = stmt.where(Post.site_id == site_id)
    if only_summarized:
        stmt = stmt.where(Post.summary.is_not(None))

    if rule == "score":
        stmt = stmt.order_by(
            Post.ai_score.desc().nullslast(), Post.created.desc().nullslast()
        )
    elif rule == "updated":
        stmt = stmt.order_by(Post.updated.desc().nullslast(), Post.id.desc())
    else:
        stmt = stmt.order_by(Post.created.desc().nullslast(), Post.id.desc())

    total = (
        await db.execute(
            select(func.count(Post.id))
            .select_from(Post)
            .where(Post.deleted_at.is_(None))
        )
    ).scalar_one()

    # end=-1 表示不限。旧工具这里传负数给 $limit 会让 MongoDB 报错，
    # 这里显式处理掉——那正是 CHANGELOG 里记的 v6.0.6 修的 bug。
    limit = None if end == -1 else max(1, end - start + 1)
    stmt = stmt.offset(start)
    if limit is not None:
        stmt = stmt.limit(limit)

    rows = (await db.execute(stmt)).all()
    data = [
        V1Post(
            title=post.title,
            link=post.link,
            created=post.created,
            updated=post.updated,
            author=site_name,
            avatar=avatar,
            summary=post.summary,
            score=post.ai_score,
        )
        for post, site_name, avatar in rows
    ]

    return V1ListResponse(
        data=data,
        total=total,
        start=start,
        end=start + len(data) - 1 if data else start,
    )


@v1_router.get("/post", response_model=list[V1Post], summary="某站点的文章")
async def v1_post(
    user: CurrentUserKey,
    db: DbSession,
    site_id: Annotated[int, Query(ge=1)],
    num: Annotated[int, Query(ge=1, le=50)] = 10,
) -> list[V1Post]:
    rows = (
        await db.execute(
            select(Post, Site.name, Site.avatar)
            .join(Site, Post.site_id == Site.id)
            .where(Post.site_id == site_id, Post.deleted_at.is_(None))
            .order_by(Post.created.desc().nullslast())
            .limit(num)
        )
    ).all()
    return [
        V1Post(
            title=p.title,
            link=p.link,
            created=p.created,
            updated=p.updated,
            author=name,
            avatar=avatar,
            summary=p.summary,
            score=p.ai_score,
        )
        for p, name, avatar in rows
    ]


@v1_router.get("/friends", summary="友链列表")
async def v1_friends(user: CurrentUserKey, db: DbSession) -> list[dict]:
    rows = (
        await db.execute(select(Site).order_by(Site.name))
    ).scalars().all()
    return [
        {
            "name": s.name,
            "link": s.url,
            "avatar": s.avatar,
            "error": s.error,
            "last_success_at": s.last_success_at,
        }
        for s in rows
    ]


@v1_router.get("/stats", summary="统计概览")
async def v1_stats(user: CurrentUserKey, db: DbSession) -> dict:
    sites_total = (await db.execute(select(func.count(Site.id)))).scalar_one()
    sites_ok = (
        await db.execute(
            select(func.count(Site.id)).where(Site.error.is_(False))
        )
    ).scalar_one()
    posts_total = (
        await db.execute(select(func.count(Post.id))).where(
            Post.deleted_at.is_(None)
        )
    ).scalar_one()
    summarized = (
        await db.execute(
            select(func.count(Post.id)).where(Post.summary.is_not(None))
        )
    ).scalar_one()
    last_updated = (
        await db.execute(
            select(func.max(Post.created_at)).where(Post.deleted_at.is_(None))
        )
    ).scalar_one()

    return {
        "friends_num": sites_total,
        "active_num": sites_ok,
        "error_num": sites_total - sites_ok,
        "article_num": posts_total,
        "summarized_num": summarized,
        "last_updated_time": last_updated,
    }