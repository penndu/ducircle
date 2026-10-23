"""后台管理接口：手动触发抓取、查看抓取状态与统计。"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import func, select

from app.api.deps import CurrentUserSession, DbSession
from app.models import CrawlLog, Post, Site

router = APIRouter(prefix="/api/admin", tags=["后台管理"])


class CrawlTriggerResponse(BaseModel):
    ok: bool
    message: str
    site_id: int | None = None


class CrawlLogOut(BaseModel):
    id: int
    site_url: str
    status: str
    strategy: str | None
    error: str | None
    duration_ms: int
    post_count: int
    http_status: int | None
    created_at: datetime


class FailedSiteOut(BaseModel):
    id: int
    name: str
    url: str
    error: bool
    error_message: str | None
    consecutive_failures: int
    last_crawled_at: datetime | None
    last_success_at: datetime | None


@router.post(
    "/sites/{site_id}/crawl",
    response_model=CrawlTriggerResponse,
    summary="立即抓取某个站点",
)
async def trigger_crawl(
    site_id: int,
    user: CurrentUserSession,
    db: DbSession,
    background: BackgroundTasks,
) -> CrawlTriggerResponse:
    """把站点标记为待抓，并在后台立刻执行。

    用 BackgroundTasks 而非直接 await：一次抓取可能要几十秒，
    让 HTTP 请求等在那里会超时，而且用户只想知道「已触发」。
    """
    site = (
        await db.execute(select(Site).where(Site.id == site_id))
    ).scalar_one_or_none()
    if site is None:
        raise HTTPException(status_code=404, detail="站点不存在")

    # 从库中读出的 active 一定是布尔值，但用 is False 与 crawler.py 保持一致，
    # 避免将来有人改成内存对象时把 None 误判为已停用
    if site.active is False:
        raise HTTPException(status_code=400, detail="站点已停用，请先启用")

    from app.services.crawl_service import crawl_single_site

    background.add_task(_run_crawl, user.id, site_id)

    return CrawlTriggerResponse(
        ok=True, message="抓取任务已触发，稍后刷新查看结果", site_id=site_id
    )


async def _run_crawl(user_id: int, site_id: int) -> None:
    """后台抓取。异常在这里被吞掉——没人接收返回值，抛出只会污染日志。"""
    import logging

    logger = logging.getLogger("ducircle.admin")
    from app.services.crawl_service import crawl_single_site

    try:
        result = await crawl_single_site(user_id, site_id)
        logger.info(
            "手动抓取完成：site=%d status=%s strategy=%s posts=%d",
            site_id, result.status, result.strategy, result.post_count,
        )
    except Exception:  # noqa: BLE001
        logger.exception("手动抓取失败：site=%d", site_id)


@router.post("/crawl/all", response_model=CrawlTriggerResponse, summary="立即抓取全部站点")
async def trigger_crawl_all(
    user: CurrentUserSession, background: BackgroundTasks
) -> CrawlTriggerResponse:
    """触发我的全部站点抓取（只抓我自己名下的）。"""
    from app.services.crawl_service import crawl_all_pending

    async def _run() -> None:
        import logging

        logger = logging.getLogger("ducircle.admin")
        try:
            await crawl_all_pending()
        except Exception:  # noqa: BLE001
            logger.exception("手动批量抓取失败")

    background.add_task(_run)
    return CrawlTriggerResponse(ok=True, message="已触发全部站点抓取")


@router.get(
    "/crawl-logs", response_model=list[CrawlLogOut], summary="抓取日志"
)
async def crawl_logs(
    user: CurrentUserSession,
    db: DbSession,
    only_failed: Annotated[bool, Query(description="只看失败记录")] = False,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[CrawlLogOut]:
    """最近的抓取记录。这是「错误透明」需求的核心——不翻服务器日志
    就能知道哪个站坏了、为什么坏。"""
    stmt = select(CrawlLog).order_by(CrawlLog.created_at.desc()).limit(limit)
    if only_failed:
        stmt = stmt.where(CrawlLog.status == "failed")
    rows = (await db.execute(stmt)).scalars().all()
    return [CrawlLogOut.model_validate(r) for r in rows]


@router.get(
    "/failed-sites", response_model=list[FailedSiteOut], summary="抓取失败的站点"
)
async def failed_sites(
    user: CurrentUserSession, db: DbSession
) -> list[FailedSiteOut]:
    """抓取失败的站点列表，用户在这里配自定义规则补救。"""
    rows = (
        await db.execute(
            select(Site)
            .where(Site.error.is_(True))
            .order_by(Site.consecutive_failures.desc(), Site.last_crawled_at.desc())
        )
    ).scalars().all()
    return [FailedSiteOut.model_validate(s) for s in rows]


@router.get("/stats", summary="我的统计概览")
async def my_stats(
    user: CurrentUserSession, db: DbSession
) -> dict:
    """活跃度统计。需求 Q2-B「谁还活着、谁已失联」的落地。

    统计逻辑本体在 compute_user_stats，页面路由（web_pages.dashboard）
    也直接调用它——直接调用 API 层函数不会触发 FastAPI 的依赖注入，
    因此调用方必须自己保证 db 已注入、租户上下文已设置。
    """
    return await compute_user_stats(db)


async def compute_user_stats(db) -> dict:
    """统计逻辑。与路由分离，供页面、API、定时任务共用。"""
    sites_total = (
        await db.execute(select(func.count(Site.id)))
    ).scalar_one()
    sites_active = (
        await db.execute(
            select(func.count(Site.id)).where(Site.active.is_(True), Site.error.is_(False))
        )
    ).scalar_one()
    sites_error = (
        await db.execute(
            select(func.count(Site.id)).where(Site.error.is_(True))
        )
    ).scalar_one()

    posts_total = (
        await db.execute(
            select(func.count(Post.id)).where(Post.deleted_at.is_(None))
        )
    ).scalar_one()
    posts_summarized = (
        await db.execute(
            select(func.count(Post.id)).where(
                Post.deleted_at.is_(None), Post.summary.is_not(None)
            )
        )
    ).scalar_one()

    # 策略命中分布——用户能直观看到系统靠什么抓到他的文章
    strategy_rows = (
        await db.execute(
            select(Post.strategy, func.count(Post.id))
            .where(Post.deleted_at.is_(None))
            .group_by(Post.strategy)
        )
    ).all()

    # 抓取成功率
    total_logs = (
        await db.execute(select(func.count(CrawlLog.id)))
    ).scalar_one()
    failed_logs = (
        await db.execute(
            select(func.count(CrawlLog.id)).where(CrawlLog.status == "failed")
        )
    ).scalar_one()

    success_rate = None
    if total_logs:
        success_rate = round((total_logs - failed_logs) / total_logs * 100, 2)

    return {
        "sites": {
            "total": sites_total,
            "active": sites_active,
            "error": sites_error,
            "zombie_rate": round(sites_error / sites_total * 100, 2) if sites_total else 0,
        },
        "posts": {
            "total": posts_total,
            "summarized": posts_summarized,
            "summary_rate": (
                round(posts_summarized / posts_total * 100, 2) if posts_total else 0
            ),
        },
        "crawl": {
            "total_runs": total_logs,
            "failed_runs": failed_logs,
            "success_rate": success_rate,
        },
        "strategy_distribution": {s or "unknown": c for s, c in strategy_rows},
    }


@router.get("/platform-stats", summary="全站统计（管理员）")
async def platform_stats(user: CurrentUserSession) -> dict:
    """全站概况。从 Redis 快照读，读不到则实时计算。"""
    from app.services.stats_cache import load_snapshot

    snapshot = await load_snapshot()
    if snapshot:
        return snapshot

    return {"error": "统计快照尚未生成，请等待定时任务或联系管理员"}