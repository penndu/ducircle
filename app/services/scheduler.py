"""定时任务调度器。

用 APScheduler 而非 Celery：几百人规模用不上 Celery 的分布式能力，
而 APScheduler 不需要额外中间件，部署时少一个组件少一份故障点。

多 worker 下的重复执行问题由 Redis 分布式锁解决——
每个任务执行前先抢锁，抢不到就直接跳过。
"""

from __future__ import annotations

import asyncio
import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from app.core.config import get_settings
from app.core.ratelimit import DistributedLock

logger = logging.getLogger("ducircle.scheduler")

_scheduler: AsyncIOScheduler | None = None


def get_scheduler() -> AsyncIOScheduler:
    global _scheduler
    if _scheduler is None:
        _scheduler = AsyncIOScheduler(timezone="Asia/Shanghai")
    return _scheduler


def _cron(expression: str) -> CronTrigger:
    """解析 cron 表达式。非法表达式不能让应用起不来，
    退回每天凌晨 1 点并记警告——服务可用性优先于调度精确度。"""
    try:
        return CronTrigger.from_crontab(expression, timezone="Asia/Shanghai")
    except (ValueError, TypeError) as exc:
        logger.error("CRON 表达式无效: %r (%s)，退回每天 01:00", expression, exc)
        return CronTrigger(hour=1, minute=0, timezone="Asia/Shanghai")


async def _run_locked(name: str, coro_factory) -> None:
    """包一层分布式锁，防止多 worker 重复执行同一任务。"""
    async with DistributedLock(name, ttl=1800) as redis:
        if redis is None:
            logger.info("任务 %s 已在其他实例执行，本次跳过", name)
            return
        try:
            await coro_factory()
        except Exception:  # noqa: BLE001
            # 单个任务失败不能让调度器整体停摆
            logger.exception("任务 %s 执行失败", name)


async def start_scheduler() -> AsyncIOScheduler:
    """注册并启动所有定时任务。"""
    settings = get_settings()
    scheduler = get_scheduler()

    if scheduler.running:
        return scheduler

    if settings.scheduler_enabled:
        scheduler.add_job(
            lambda: _run_locked("daily-crawl", run_crawl_all),
            _cron(settings.crawl_cron),
            id="crawl_all",
            name="定时抓取",
            replace_existing=True,
            # 抓取可能持续很久，超时给足
            misfire_grace_time=3600,
            coalesce=True,  # 错过的多次触发合并成一次，不补跑
            max_instances=1,
        )

        scheduler.add_job(
            lambda: _run_locked("cleanup", run_cleanup),
            _cron(settings.cleanup_cron),
            id="cleanup",
            name="过期数据清理",
            replace_existing=True,
            misfire_grace_time=3600,
            coalesce=True,
            max_instances=1,
        )

        scheduler.add_job(
            lambda: _run_locked("ai-summary", run_ai_summaries),
            _cron(settings.ai_cron),
            id="ai_summary",
            name="AI 摘要生成",
            replace_existing=True,
            misfire_grace_time=3600,
            coalesce=True,
            max_instances=1,
        )

        scheduler.add_job(
            lambda: _run_locked("stats", run_stats_snapshot),
            _cron(settings.stats_cron),
            id="stats",
            name="统计快照",
            replace_existing=True,
            misfire_grace_time=3600,
            coalesce=True,
            max_instances=1,
        )

        scheduler.start()
        logger.info(
            "调度器已启动，任务：%s",
            ", ".join(j.name for j in scheduler.get_jobs()),
        )
    else:
        logger.info("调度器未启用（SCHEDULER_ENABLED=false）")

    return scheduler


async def shutdown_scheduler() -> None:
    global _scheduler
    if _scheduler is not None and _scheduler.running:
        _scheduler.shutdown(wait=False)
        logger.info("调度器已停止")
    _scheduler = None


# ---- 具体任务 ----
# 放在文件末尾导入，避免循环依赖：任务模块要导入 db，db 不依赖本模块


async def run_crawl_all() -> dict:
    """抓取所有用户所有待抓站点。"""
    from app.services.crawl_service import crawl_all_pending

    return await crawl_all_pending()


async def run_cleanup() -> dict:
    """清理过期文章与软删除记录。"""
    from app.services.crawl_service import cleanup_expired

    return await cleanup_expired()


async def run_ai_summaries() -> dict:
    """为所有用户的待处理文章生成摘要。"""
    from app.services.ai_summary import summarize_all_pending

    return await summarize_all_pending()


async def run_stats_snapshot() -> dict:
    """生成统计快照，供后台看板快速读取。"""
    from app.services.crawl_service import build_stats_snapshot

    return await build_stats_snapshot()