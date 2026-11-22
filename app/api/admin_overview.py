"""管理员总览接口（仅管理员）。

页面和 JSON 接口共用 build_admin_overview——
两处各写一套 count 的话，早晚会出现「页面显示 100、接口说 98」。
"""

from __future__ import annotations

from fastapi import APIRouter

from app.api.deps import AdminUser, DbSession
from app.services.admin_overview import build_admin_overview

router = APIRouter(prefix="/api/admin/overview", tags=["管理员总览"])


@router.get("", summary="全站总览数据")
async def overview(_admin: AdminUser, db: DbSession) -> dict:
    return await build_admin_overview(db)
