"""兑换码管理接口（仅管理员）。

三个硬约束和 ``admin_users.py`` 完全一致，理由也一样：

1. **一律走 ``AdminUser`` 依赖** —— 普通用户 403、未登录 401。
   入口在侧边栏隐藏只是顺手，不是安全措施。
2. **生成次数有上限** —— 手滑多打一个 0 不该变成 5000 张有效码。
3. **作废只能针对未使用的码** —— 已使用的码作废等于收回用户已付钱的权益，
   那要走退款流程，不是点一下按钮。
"""

from __future__ import annotations

import logging
from datetime import datetime

from fastapi import APIRouter, HTTPException, Query, status
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from app.api.deps import AdminUser, DbSession
from app.models import RedeemBatch, RedeemCode
from app.services.redeem import (
    MAX_BATCH_QUANTITY,
    PLAN_ORDER,
    RedeemError,
    batch_stats,
    create_batch,
    plan_catalog,
    price_yuan,
    revoke_code,
)

router = APIRouter(prefix="/api/admin/redeem-codes", tags=["兑换码管理"])
logger = logging.getLogger("ducircle.admin_redeem")


# ---- 输出模型 ----


class PlanOut(BaseModel):
    key: str
    label: str
    credits: int
    price_cents: int
    summary: str


class BatchOut(BaseModel):
    id: int
    batch_no: str
    plan: str
    plan_label: str
    credits: int
    price_cents: int
    quantity: int
    note: str | None
    created_by_name: str | None
    created_at: datetime
    used: int = 0
    unused: int = 0
    revoked: int = 0
    face_value_yuan: str = "0"


class BatchCreateIn(BaseModel):
    plan: str = Field(min_length=1, max_length=16)
    quantity: int = Field(ge=1, le=MAX_BATCH_QUANTITY)
    note: str | None = Field(default=None, max_length=255)


class BatchCreateOut(BaseModel):
    batch: BatchOut
    codes: list[str]


class CodeOut(BaseModel):
    id: int
    code: str
    batch_id: int | None
    plan: str
    plan_label: str
    credits: int
    price_cents: int
    status: str
    used_by_name: str | None
    used_at: datetime | None
    revoked_at: datetime | None
    revoke_reason: str | None
    note: str | None
    created_at: datetime


class CodeListOut(BaseModel):
    items: list[CodeOut]
    total: int
    limit: int
    offset: int
    # 当前筛选下的分类计数，界面用来显示「未用 37 / 已用 63」
    counts: dict[str, int]


class RevokeIn(BaseModel):
    reason: str | None = Field(default=None, max_length=255)


# ---- 辅助 ----


def _plan_label(plan: str) -> str:
    for item in plan_catalog():
        if item["key"] == plan:
            return item["label"]
    return plan


def _to_batch_out(batch: RedeemBatch, stats: dict) -> BatchOut:
    return BatchOut(
        id=batch.id,
        batch_no=batch.batch_no,
        plan=batch.plan,
        plan_label=_plan_label(batch.plan),
        credits=batch.credits,
        price_cents=batch.price_cents,
        quantity=batch.quantity,
        note=batch.note,
        created_by_name=batch.created_by_name,
        created_at=batch.created_at,
        used=stats.get("used", 0),
        unused=stats.get("unused", batch.quantity),
        revoked=stats.get("revoked", 0),
        face_value_yuan=price_yuan(batch.price_cents * batch.quantity),
    )


def _to_code_out(row: RedeemCode) -> CodeOut:
    return CodeOut(
        id=row.id,
        code=row.code,
        batch_id=row.batch_id,
        plan=row.plan,
        plan_label=_plan_label(row.plan),
        credits=row.credits,
        price_cents=row.price_cents,
        status=row.status,
        used_by_name=row.used_by_name,
        used_at=row.used_at,
        revoked_at=row.revoked_at,
        revoke_reason=row.revoke_reason,
        note=row.note,
        created_at=row.created_at,
    )


def _state_conditions(status_filter: str) -> list:
    if status_filter == "unused":
        return [RedeemCode.used_by.is_(None), RedeemCode.revoked_at.is_(None)]
    if status_filter == "used":
        return [RedeemCode.used_by.is_not(None)]
    if status_filter == "revoked":
        return [RedeemCode.revoked_at.is_not(None)]
    return []


async def query_batches(db, *, limit: int = 50, offset: int = 0):
    """批次列表（带使用统计）。页面与 JSON 接口共用，保证两边一致。"""
    total = (await db.execute(select(func.count(RedeemBatch.id)))).scalar_one()
    rows = (
        await db.execute(
            select(RedeemBatch)
            .order_by(RedeemBatch.id.desc())
            .limit(limit)
            .offset(offset)
        )
    ).scalars().all()
    stats = await batch_stats(db, [row.id for row in rows])
    return [_to_batch_out(row, stats.get(row.id, {})) for row in rows], int(total)


async def query_codes(
    db,
    *,
    status_filter: str = "all",
    plan: str = "all",
    batch_id: int | None = None,
    keyword: str | None = None,
    limit: int = 50,
    offset: int = 0,
):
    """兑换码列表。页面和 JSON 接口共用。"""
    conditions = list(_state_conditions(status_filter))
    if plan in PLAN_ORDER:
        conditions.append(RedeemCode.plan == plan)
    if batch_id:
        conditions.append(RedeemCode.batch_id == batch_id)
    if keyword:
        conditions.append(RedeemCode.code.contains(keyword.strip().upper()))

    count_stmt = select(func.count(RedeemCode.id))
    if conditions:
        count_stmt = count_stmt.where(*conditions)
    total = (await db.execute(count_stmt)).scalar_one()

    stmt = select(RedeemCode)
    if conditions:
        stmt = stmt.where(*conditions)
    rows = (
        await db.execute(
            stmt.order_by(RedeemCode.id.desc()).limit(limit).offset(offset)
        )
    ).scalars().all()

    # 分类计数固定按 status 统计（不受 status 筛选影响，否则永远是 0）
    base = []
    if plan in PLAN_ORDER:
        base.append(RedeemCode.plan == plan)
    if batch_id:
        base.append(RedeemCode.batch_id == batch_id)
    if keyword:
        base.append(RedeemCode.code.contains(keyword.strip().upper()))

    async def _count(extra: list) -> int:
        stmt_c = select(func.count(RedeemCode.id))
        conds = base + extra
        if conds:
            stmt_c = stmt_c.where(*conds)
        return int((await db.execute(stmt_c)).scalar_one())

    counts = {
        "all": await _count([]),
        "unused": await _count(
            [RedeemCode.used_by.is_(None), RedeemCode.revoked_at.is_(None)]
        ),
        "used": await _count([RedeemCode.used_by.is_not(None)]),
        "revoked": await _count([RedeemCode.revoked_at.is_not(None)]),
    }

    return [_to_code_out(row) for row in rows], int(total), counts


# ---- 接口 ----


@router.get("/plans", response_model=list[PlanOut], summary="可生成的兑换码类型")
async def list_plans(user: AdminUser) -> list[PlanOut]:
    """定价目录。前端据此渲染下拉框，避免价格写死在模板里两处。"""
    return [PlanOut(**item) for item in plan_catalog()]


@router.post(
    "/batches",
    response_model=BatchCreateOut,
    status_code=status.HTTP_201_CREATED,
    summary="批量生成兑换码",
)
async def create_batch_codes(
    payload: BatchCreateIn,
    user: AdminUser,
    db: DbSession,
) -> BatchCreateOut:
    try:
        batch, codes = await create_batch(
            db,
            admin=user,
            plan=payload.plan,
            quantity=payload.quantity,
            note=(payload.note or "").strip() or None,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    await db.commit()
    return BatchCreateOut(
        batch=_to_batch_out(
            batch,
            {
                "used": 0,
                "unused": batch.quantity,
                "revoked": 0,
            },
        ),
        codes=[row.code for row in codes],
    )


@router.get("/batches", response_model=list[BatchOut], summary="批次列表")
async def list_batches(
    user: AdminUser,
    db: DbSession,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> list[BatchOut]:
    items, _ = await query_batches(db, limit=limit, offset=offset)
    return items


@router.get("/codes", response_model=CodeListOut, summary="兑换码列表")
async def list_codes(
    user: AdminUser,
    db: DbSession,
    status_filter: str = Query(
        default="all",
        alias="status",
        pattern="^(all|unused|used|revoked)$",
    ),
    plan: str = Query(default="all", pattern="^(all|trial|standard)$"),
    batch_id: int | None = Query(default=None, ge=1),
    q: str | None = Query(default=None, max_length=32, description="按兑换码搜索"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> CodeListOut:
    items, total, counts = await query_codes(
        db,
        status_filter=status_filter,
        plan=plan,
        batch_id=batch_id,
        keyword=q,
        limit=limit,
        offset=offset,
    )
    return CodeListOut(
        items=items, total=total, limit=limit, offset=offset, counts=counts
    )


@router.post("/codes/{code_id}/revoke", response_model=CodeOut, summary="作废兑换码")
async def revoke(
    code_id: int,
    payload: RevokeIn,
    user: AdminUser,
    db: DbSession,
) -> CodeOut:
    try:
        row = await revoke_code(
            db, code_id=code_id, admin=user, reason=payload.reason
        )
    except RedeemError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    await db.commit()
    return _to_code_out(row)


@router.get(
    "/export",
    response_class=PlainTextResponse,
    summary="导出兑换码（纯文本，一行一张）",
)
async def export_codes(
    user: AdminUser,
    db: DbSession,
    batch_id: int = Query(..., ge=1),
    include_used: bool = Query(default=False, description="是否包含已使用/已作废的码"),
) -> PlainTextResponse:
    """导出给买家或导入到别处用。

    默认只导**还没被用掉**的码——把已使用的码发出去会让买家兑换时报「已被使用」，
    徒增沟通成本。
    """
    batch = await db.get(RedeemBatch, batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail="批次不存在")

    conditions = [RedeemCode.batch_id == batch_id]
    if not include_used:
        conditions += _state_conditions("unused")

    rows = (
        await db.execute(
            select(RedeemCode).where(*conditions).order_by(RedeemCode.id)
        )
    ).scalars().all()

    lines = [row.code for row in rows]
    header = f"# 批次 {batch.batch_no} 面值 {batch.credits} 次/张 共 {len(lines)} 张"

    batch.exported = True
    await db.commit()

    return PlainTextResponse(
        "\n".join([header, *lines]) + "\n",
        headers={
            "Content-Disposition": (
                f'attachment; filename="redeem-{batch.batch_no}.txt"'
            )
        },
    )
