"""AI 摘要相关接口。"""

from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import func, select

from app.api.deps import CurrentUserSession, DbSession
from app.models import AIQuotaLog, Post
from app.services.ai_summary import (
    AIKeyRequired,
    QuotaExceeded,
    summarize_single,
)

router = APIRouter(prefix="/api/ai", tags=["AI 摘要"])


class QuotaOut(BaseModel):
    enabled: bool
    has_own_key: bool
    used_today: int
    daily_limit: int
    remaining: int
    # 平台不提供免费额度时为 True——前端据此提示「去填 Key」
    key_required: bool = False


class SummarizeResultOut(BaseModel):
    ok: bool
    summary: str | None = None
    score: float | None = None
    model: str | None = None
    reason: str | None = None


@router.get("/quota", response_model=QuotaOut, summary="我的 AI 额度")
async def my_quota(user: CurrentUserSession, db: DbSession) -> QuotaOut:
    """查询今日额度使用情况。设置页展示这个。"""
    from datetime import date

    from app.core.config import get_settings
    from app.services.ai_summary import check_quota

    allowed, used, limit = await check_quota(user, db)
    settings = get_settings()

    remaining = None if user.has_own_ai_key else max(0, limit - used)

    return QuotaOut(
        enabled=settings.ai_enabled and user.ai_enabled,
        has_own_key=user.has_own_ai_key,
        used_today=used,
        daily_limit=limit,
        remaining=remaining,
        key_required=(
            not user.has_own_ai_key and limit <= 0 and user.ai_enabled
        ),
    )


@router.get("/usage", summary="额度使用历史")
async def usage_history(
    user: CurrentUserSession,
    db: DbSession,
    days: int = Query(default=14, ge=1, le=90),
) -> list[dict]:
    """最近 N 天的额度消耗。用于画出使用趋势。"""
    from datetime import date, timedelta

    since = date.today() - timedelta(days=days)

    rows = (
        await db.execute(
            select(AIQuotaLog.quota_date, func.count(AIQuotaLog.id))
            .where(
                AIQuotaLog.user_id == user.id,
                AIQuotaLog.quota_date >= since,
                AIQuotaLog.from_user_key.is_(False),
            )
            .group_by(AIQuotaLog.quota_date)
            .order_by(AIQuotaLog.quota_date)
        )
    ).all()

    # 补齐没有记录的日期，折线图才不会断裂
    counts = dict(rows)
    today = date.today()
    return [
        {
            "date": (since + timedelta(days=i)).isoformat(),
            "count": counts.get(since + timedelta(days=i), 0),
        }
        for i in range(days + 1)
        if since + timedelta(days=i) <= today
    ]


@router.post(
    "/posts/{post_id}/summarize",
    response_model=SummarizeResultOut,
    summary="为单篇文章生成摘要",
)
async def trigger_summarize(
    post_id: int,
    user: CurrentUserSession,
    db: DbSession,
) -> SummarizeResultOut:
    """立即为指定文章生成摘要。

    会真的去抓文章页面（可能耗时十几秒），因此同步返回而不是后台任务——
    用户点按钮就是想看到结果。
    """
    from app.models import User as UserModel

    post = (
        await db.execute(select(Post).where(Post.id == post_id))
    ).scalar_one_or_none()
    if post is None:
        raise HTTPException(status_code=404, detail="文章不存在")

    user_model = await db.get(UserModel, user.id)
    try:
        result = await summarize_single(user.id, post_id)
    except AIKeyRequired as exc:
        # 403 而不是 429：这不是「等一会儿再来」，是「去填个 Key」
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except QuotaExceeded as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    return SummarizeResultOut(**result)


@router.get("/batch-status", summary="摘要生成的状态")
async def batch_summary_status(user: CurrentUserSession) -> dict:
    """批量摘要任务的进度与结果。

    前端轮询这个接口显示「跑到第几篇、成不成功、为什么失败」，
    而不是只弹一句「已触发」就让人干等。
    """
    from app.services.summary_status import get_status

    status = await get_status(user.id)
    if status is None:
        return {"state": "idle"}
    return status


@router.post("/batch", summary="批量生成待处理文章的摘要")
async def batch_summarize(
    user: CurrentUserSession,
    db: DbSession,
    limit: int = Query(default=20, ge=1, le=100),
    background: BackgroundTasks = None,  # type: ignore[assignment]
) -> dict:
    """为所有「还没有摘要」的文章生成摘要。

    放后台执行——可能要跑几分钟，用户不该干等。
    """
    from app.services.ai_summary import check_quota, summarize_pending_for_user

    # 前置拦截：平台不提供免费额度时，没填 Key 就直接说清楚，
    # 别让用户点了按钮等半天，最后看到一句「生成失败」。
    #
    # ⚠️ 别把 check_quota 返回的配额上限写回 limit 变量——
    # 那会把「要处理几篇」变成「每日额度上限」，用户自带 Key 时额度上限是 0，
    # 结果一篇都不处理，接口还返回 200 说「已开始」。
    # 两个值含义不同，名字要分开。
    allowed, used, quota_limit = await check_quota(user, db)
    if not allowed and quota_limit <= 0 and not user.has_own_ai_key:
        raise HTTPException(
            status_code=403,
            detail=(
                "生成摘要需要你自己的 API Key。到「设置 → AI 摘要」填一个，"
                "填完立即生效，额度由你自己掌握。"
            ),
        )

    background.add_task(summarize_pending_for_user, user.id, limit)
    return {"ok": True, "message": f"已开始为最多 {limit} 篇文章生成摘要"}


@router.post("/posts/{post_id}/reset", summary="清除文章摘要")
async def reset_summary(
    post_id: int, user: CurrentUserSession, db: DbSession
) -> dict:
    """清除摘要以便重新生成。也会解锁人工修正状态。"""
    post = (
        await db.execute(select(Post).where(Post.id == post_id))
    ).scalar_one_or_none()
    if post is None:
        raise HTTPException(status_code=404, detail="文章不存在")

    post.summary = None
    post.ai_score = None
    post.ai_reason = None
    post.summary_content_hash = None
    post.summary_edited = False
    # 一并清掉「永久失败」标记——用户手动点了就是想让重试
    post.summary_failed = False
    post.summary_error = None
    return {"ok": True}


@router.patch("/posts/{post_id}", summary="手动修正摘要")
async def edit_summary(
    post_id: int,
    user: CurrentUserSession,
    db: DbSession,
    summary: str = Query(..., min_length=1, max_length=1000),
    score: float | None = Query(None, ge=0, le=10),
) -> dict:
    """人工修正摘要。

    修正后会被标记 summary_edited，自动任务不再覆盖——
    用户的判断优先于模型的判断。
    """
    post = (
        await db.execute(select(Post).where(Post.id == post_id))
    ).scalar_one_or_none()
    if post is None:
        raise HTTPException(status_code=404, detail="文章不存在")

    post.summary = summary.strip()
    post.summary_edited = True
    if score is not None:
        post.ai_score = score
    return {"ok": True, "summary": post.summary}