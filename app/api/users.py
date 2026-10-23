"""用户设置与数据管理接口。"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Body, HTTPException, Query, Response
from pydantic import BaseModel
from sqlalchemy import delete, select

from app.api.deps import CurrentUserSession, DbSession
from app.core.security import generate_api_key, hash_api_key
from app.models import CrawlLog, Post, Site, SiteRule

router = APIRouter(prefix="/api/users", tags=["用户设置"])


class UserSettingsUpdate(BaseModel):
    ai_enabled: bool | None = None
    ai_provider: str | None = None
    ai_model: str | None = None
    ai_api_key: str | None = None
    max_concurrent_override: int | None = None
    ai_daily_quota_override: int | None = None


@router.patch("/me", summary="更新我的设置")
async def update_settings(
    payload: UserSettingsUpdate,
    user: CurrentUserSession,
    db: DbSession,
) -> dict:
    data = payload.model_dump(exclude_unset=True)

    # 额度与并发必须为正数，否则会导致抓取任务永远排不上队
    if "max_concurrent_override" in data and data["max_concurrent_override"] is not None:
        if not 1 <= data["max_concurrent_override"] <= 50:
            raise HTTPException(status_code=400, detail="并发上限应在 1~50 之间")
    if "ai_daily_quota_override" in data and data["ai_daily_quota_override"] is not None:
        if not 0 <= data["ai_daily_quota_override"] <= 1000:
            raise HTTPException(status_code=400, detail="每日额度应在 0~1000 之间")

    for field, value in data.items():
        setattr(user, field, value)

    await db.flush()
    return {
        "ok": True,
        "ai_enabled": user.ai_enabled,
        "ai_provider": user.ai_provider,
        "has_own_ai_key": user.has_own_ai_key,
    }


@router.delete("/me/data", summary="清空我的全部数据")
async def purge_my_data(
    user: CurrentUserSession,
    db: DbSession,
    confirm: Annotated[bool, Query(description="必须传 true，防误触")] = False,
) -> dict:
    """删除我的全部站点、文章、规则和日志。

    这是「数据属于用户」的最终保障——用户有权一键清空，
    也满足站点博主要求下架时的合规需求。
    """
    if not confirm:
        raise HTTPException(status_code=400, detail="需要 confirm=true 参数确认")

    # 先取 id 列表，避免 ORM 钩子在批量删除时误判
    site_ids = (await db.execute(select(Site.id))).scalars().all()
    post_ids = (await db.execute(select(Post.id))).scalars().all()
    rule_ids = (await db.execute(select(SiteRule.id))).scalars().all()
    log_ids = (await db.execute(select(CrawlLog.id))).scalars().all()

    if not site_ids and not post_ids:
        return {"deleted_sites": 0, "deleted_posts": 0, "ok": True}

    # 按依赖顺序删：先子表再主表
    if rule_ids:
        await db.execute(delete(SiteRule).where(SiteRule.id.in_(rule_ids)))
    if post_ids:
        await db.execute(delete(Post).where(Post.id.in_(post_ids)))
    if log_ids:
        await db.execute(delete(CrawlLog).where(CrawlLog.id.in_(log_ids)))
    if site_ids:
        await db.execute(delete(Site).where(Site.id.in_(site_ids)))

    return {
        "ok": True,
        "deleted_sites": len(site_ids),
        "deleted_posts": len(post_ids),
        "deleted_rules": len(rule_ids),
    }


@router.post("/me/api-key", summary="重新生成 API Key")
async def reset_api_key(user: CurrentUserSession, db: DbSession) -> dict:
    key = generate_api_key()
    user.api_key_hash = hash_api_key(key)
    await db.flush()
    return {"api_key": key}