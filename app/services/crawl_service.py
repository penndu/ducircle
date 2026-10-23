"""抓取服务：调度器与抓取引擎之间的胶水层。

职责边界：
- crawler.py 只管「怎么解析一个页面」，不碰数据库
- 本模块管「哪些站点该抓、怎么并发、抓完怎么落库、失败怎么记」

这样分层的好处是 crawler 可以脱离数据库独立测试
（scripts/live_check.py 就是这么用的）。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta

from sqlalchemy import func, select, update

from app.core.config import get_settings
from app.core.ratelimit import RedisManager, Semaphore
from app.core.tenant import as_tenant_async, no_tenant_filter
from app.db import session_scope
from app.models import CrawlLog, Post, Site, User
from app.parser.base import CrawlResult
from app.services.crawler import Crawler, save_crawl_result, should_crawl

logger = logging.getLogger("fcircle.crawl")


async def crawl_all_pending(max_sites: int | None = None) -> dict:
    """抓取所有用户所有到期站点。

    用 as_tenant_async 逐用户切换上下文，让 ORM 钩子自动做隔离——
    调度器不需要也不能手工拼 user_id 条件。

    单用户内部用信号量限并发，全局再套一层并发上限（见 Crawler）。
    """
    settings = get_settings()

    # 系统级查询：跨用户，需要显式豁免
    async with session_scope() as db, no_tenant_filter():
        users = (
            await db.execute(select(User.id).where(User.is_active.is_(True)))
        ).scalars().all()

    if not users:
        return {"users": 0, "crawled": 0, "new_posts": 0}

    total_crawled = 0
    total_new = 0

    for user_id in users:
        try:
            async with as_tenant_async(user_id):
                result = await _crawl_user_sites(db_factory=session_scope, user_id=user_id)
        except Exception:  # noqa: BLE001
            logger.exception("用户 %d 的抓取任务失败", user_id)
            continue
        total_crawled += result["crawled"]
        total_new += result["new_posts"]

    logger.info(
        "定时抓取完成：%d 个用户，%d 个站点，新增 %d 篇文章",
        len(users), total_crawled, total_new,
    )
    return {"users": len(users), "crawled": total_crawled, "new_posts": total_new}


async def _crawl_user_sites(db_factory, user_id: int) -> dict:
    """抓一个用户的所有到期站点。"""
    settings = get_settings()

    async with db_factory() as db:
        # 上次成功后经过足够间隔的站点才抓，避免重复打扰对方服务器
        cutoff = datetime.now() - timedelta(minutes=settings.crawl_min_interval // 60)
        stmt = (
            select(Site)
            .where(
                Site.active.is_(True),
                (Site.last_crawled_at.is_(None)) | (Site.last_crawled_at < cutoff),
            )
            .order_by(Site.last_crawled_at.asc().nullsfirst())
        )
        if settings.outdate_clean_days:
            # 彻底不活跃的站点先跳过，把资源留给还在更新的
            zombie_cutoff = datetime.now() - timedelta(days=settings.outdate_clean_days * 3)
            stmt = stmt.where(
                (Site.last_success_at.is_(None)) | (Site.last_success_at > zombie_cutoff)
            )

        sites = (await db.execute(stmt)).scalars().all()
        if not sites:
            return {"crawled": 0, "new_posts": 0}

        redis = RedisManager.instance().client
        per_user = Semaphore(f"crawl-user-{user_id}", settings.max_concurrent_per_user)

        async with Crawler(settings, redis=redis) as crawler:
            results = await _run_bounded(
                [_crawl_one(crawler, db, site, redis, per_user) for site in sites]
            )

        crawled = sum(1 for r in results if r["status"] == "success")
        new_posts = sum(r["new_posts"] for r in results)
        return {"crawled": crawled, "new_posts": new_posts}


async def _crawl_one(
    crawler: Crawler, db, site: Site, redis, per_user: Semaphore
) -> dict:
    """抓取单个站点。每个站点独立事务，单站失败不影响其他站点。"""
    if not await per_user.acquire(redis):
        return {"status": "skipped", "new_posts": 0}

    try:
        result = await crawler.crawl_site_with_rules(db, site)
        new_posts = await save_crawl_result(db, site, result)
        await db.commit()

        if result.status == "success":
            logger.info(
                "站点 %s 抓取成功（%s），新增 %d 篇",
                site.name, result.strategy, new_posts,
            )
        else:
            logger.warning(
                "站点 %s 抓取失败：%s", site.name, result.error or "未知原因"
            )

        return {"status": result.status, "new_posts": new_posts}
    except Exception as exc:  # noqa: BLE001
        logger.exception("站点 %s 抓取异常", site.name)
        # 抓取本身崩了也要落一条失败日志，否则后台看不到
        await db.rollback()
        try:
            async with session_scope() as log_db, as_tenant_async(site.user_id):
                log_db.add(
                    CrawlLog(
                        user_id=site.user_id,
                        site_id=site.id,
                        site_url=site.url,
                        status="failed",
                        error=f"{type(exc).__name__}: {exc}",
                    )
                )
        except Exception:  # noqa: BLE001
            logger.exception("连失败日志都写不进去")
        return {"status": "failed", "new_posts": 0}
    finally:
        await per_user.release(redis)


async def _run_bounded(coros: list) -> list:
    """跑一批协程，但不让它们同时全开。

    站点可能有几百个，一次性 spawn 几百个协程会打爆连接池。
    """
    results = []
    batch_size = get_settings().max_concurrent_global
    for i in range(0, len(coros), batch_size):
        batch = coros[i : i + batch_size]
        gathered = await asyncio.gather(*batch, return_exceptions=True)
        for item in gathered:
            if isinstance(item, BaseException):
                logger.error("抓取任务异常: %s", item)
                results.append({"status": "failed", "new_posts": 0})
            else:
                results.append(item)
    return results


async def crawl_single_site(user_id: int, site_id: int) -> CrawlResult:
    """手动触发抓取单个站点。后台的「立即抓取」按钮走这里。"""
    settings = get_settings()
    redis = RedisManager.instance().client

    async with session_scope() as db, as_tenant_async(user_id):
        site = (
            await db.execute(select(Site).where(Site.id == site_id))
        ).scalar_one_or_none()
        if site is None:
            raise ValueError("站点不存在")

        async with Crawler(settings, redis=redis) as crawler:
            result = await crawler.crawl_site_with_rules(db, site)
            await save_crawl_result(db, site, result)
            return result


async def cleanup_expired() -> dict:
    """清理过期文章与软删除记录。"""
    settings = get_settings()
    if not settings.outdate_clean_days:
        return {"deleted_posts": 0, "purged_sites": 0}

    cutoff = datetime.now() - timedelta(days=settings.outdate_clean_days)

    async with session_scope() as db, no_tenant_filter():
        # 只删软删除超过 7 天的记录，保留窗口给误删恢复
        purge_cutoff = datetime.now() - timedelta(days=7)

        # 1) 清理已被软删除且过期的文章
        stale_posts = (
            await db.execute(
                select(Post).where(
                    Post.deleted_at.is_not(None), Post.deleted_at < purge_cutoff
                )
            )
        ).scalars().all()
        for post in stale_posts:
            await db.delete(post)

        # 2) 清理软删除的站点（级联删掉其文章）
        stale_sites = (
            await db.execute(
                select(Site).where(
                    Site.deleted_at.is_not(None), Site.deleted_at < purge_cutoff
                )
            )
        ).scalars().all()
        purged_sites = len(stale_sites)
        for site in stale_sites:
            await db.delete(site)

        # 3) 清理旧日志，防止表无限膨胀
        log_cutoff = datetime.now() - timedelta(days=30)
        old_logs = (
            await db.execute(
                select(CrawlLog).where(CrawlLog.created_at < log_cutoff)
            )
        ).scalars().all()
        for log in old_logs:
            await db.delete(log)

    logger.info(
        "清理完成：文章 %d、站点 %d、日志 %d",
        len(stale_posts), purged_sites, len(old_logs),
    )
    return {
        "deleted_posts": len(stale_posts),
        "purged_sites": purged_sites,
        "deleted_logs": len(old_logs),
    }


async def build_stats_snapshot() -> dict:
    """全站统计。定时任务跑，缓存起来给后台看板读。

    实时算 count 在数据量大时很慢，而看板不需要秒级实时。
    """
    from app.services.stats_cache import save_snapshot

    async with session_scope() as db, no_tenant_filter():
        snapshot = await compute_global_stats(db)
    await save_snapshot(snapshot)
    return snapshot


async def compute_global_stats(db) -> dict:
    """计算全站与分用户的统计数据。"""
    total_users = (
        await db.execute(select(func.count(User.id)).where(User.is_active.is_(True)))
    ).scalar_one()
    total_sites = (
        await db.execute(select(func.count(Site.id)).where(Site.deleted_at.is_(None)))
    ).scalar_one()
    error_sites = (
        await db.execute(
            select(func.count(Site.id)).where(
                Site.deleted_at.is_(None), Site.error.is_(True)
            )
        )
    ).scalar_one()
    total_posts = (
        await db.execute(
            select(func.count(Post.id)).where(Post.deleted_at.is_(None))
        )
    ).scalar_one()

    # 各策略命中分布——这是持续优化解析规则的核心依据
    strategy_rows = (
        await db.execute(
            select(Post.strategy, func.count(Post.id))
            .where(Post.deleted_at.is_(None))
            .group_by(Post.strategy)
        )
    ).all()
    strategy_dist: dict[str, int] = {
        (s or "unknown"): c for s, c in strategy_rows
    }

    return {
        "generated_at": datetime.now().isoformat(),
        "total_users": total_users,
        "total_sites": total_sites,
        "error_sites": error_sites,
        "total_posts": total_posts,
        "strategy_distribution": strategy_dist,
    }