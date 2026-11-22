"""管理员总览：全站数据的聚合查询。

原则：

1. **全部跨租户**——包在 ``no_tenant_filter_async()`` 里。
   认证依赖会把管理员 ID 写进租户上下文，漏掉豁免的话
   「全站统计」会变成「管理员自己的统计」，数字看着合理却全是错的。
2. **实时查，不读 Redis 快照**——快照是抓取任务写的、给普通用户看板用的，
   字段和这里需要的完全不同（兑换码 / AI 用量它都没有）。
   管理页访问频率低，几十条 count 的开销可以接受。
3. **日期用日期字段本身**——``AIQuotaLog.quota_date`` 是 Date，
   ``Post.crawled_at`` 是 DateTime，按 MySQL/SQLite 都能兼容的方式分组。
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta

from sqlalchemy import func, select

from app.core.tenant import no_tenant_filter_async
from app.models import (
    AIQuotaLog,
    CrawlLog,
    LinkPage,
    Post,
    RedeemBatch,
    RedeemCode,
    Site,
    User,
)

logger = logging.getLogger("ducircle.admin_overview")


def _day_counts(rows, days: int, today: date) -> list[dict]:
    """把 (日期, 数量) 行补成连续 days 天，缺的天数填 0。"""
    counts = {str(day)[:10]: int(n) for day, n in rows}
    out = []
    for back in range(days - 1, -1, -1):
        d = today - timedelta(days=back)
        out.append(
            {
                "date": d.isoformat(),
                "label": f"{d.month}/{d.day}",
                "count": counts.get(d.isoformat(), 0),
            }
        )
    return out


async def build_admin_overview(db) -> dict:
    """一次查出管理员总览需要的全部数字。"""
    today = date.today()
    now = datetime.now()
    today_start = datetime.combine(today, datetime.min.time())
    week_ago = now - timedelta(days=7)
    month_ago = now - timedelta(days=30)
    day_14 = today - timedelta(days=13)

    async def count(model, *conditions) -> int:
        stmt = select(func.count(model.id))
        for cond in conditions:
            stmt = stmt.where(cond)
        return int((await db.execute(stmt)).scalar_one() or 0)

    async with no_tenant_filter_async():
        # ---- 用户 ----
        users_total = await count(User)
        users_active = await count(User, User.is_active.is_(True))
        users_admin = await count(User, User.is_admin.is_(True))
        users_new_today = await count(User, User.created_at >= today_start)
        users_new_7d = await count(User, User.created_at >= week_ago)
        users_with_site = int(
            (
                await db.execute(
                    select(func.count(func.distinct(Site.user_id))).where(
                        Site.deleted_at.is_(None)
                    )
                )
            ).scalar_one()
            or 0
        )

        # ---- 友链 ----
        sites_total = await count(Site, Site.deleted_at.is_(None))
        sites_active = await count(
            Site, Site.deleted_at.is_(None), Site.active.is_(True)
        )
        sites_paused = await count(
            Site, Site.deleted_at.is_(None), Site.paused.is_(True)
        )
        sites_error = await count(
            Site, Site.deleted_at.is_(None), Site.error.is_(True)
        )
        link_pages_total = await count(LinkPage)
        link_pages_active = await count(LinkPage, LinkPage.active.is_(True))

        # ---- 文章 ----
        posts_total = await count(Post, Post.deleted_at.is_(None))
        posts_today = await count(
            Post, Post.deleted_at.is_(None), Post.crawled_at >= today_start
        )
        posts_7d = await count(
            Post, Post.deleted_at.is_(None), Post.crawled_at >= week_ago
        )
        posts_summarized = await count(
            Post, Post.deleted_at.is_(None), Post.summary.is_not(None)
        )
        posts_pending = await count(
            Post,
            Post.deleted_at.is_(None),
            Post.summary.is_(None),
            Post.summary_edited.is_(False),
            Post.summary_failed.is_(False),
        )
        posts_failed = await count(Post, Post.summary_failed.is_(True))
        posts_edited = await count(Post, Post.summary_edited.is_(True))

        # ---- 近 14 天文章收录趋势 ----
        post_trend_rows = (
            await db.execute(
                select(func.date(Post.crawled_at), func.count(Post.id))
                .where(
                    Post.deleted_at.is_(None),
                    Post.crawled_at >= datetime.combine(day_14, datetime.min.time()),
                )
                .group_by(func.date(Post.crawled_at))
            )
        ).all()
        post_trend = _day_counts(post_trend_rows, 14, today)

        # ---- AI 摘要用量 ----
        summaries_total = await count(AIQuotaLog)
        summaries_today = await count(AIQuotaLog, AIQuotaLog.quota_date == today)
        summaries_7d = await count(AIQuotaLog, AIQuotaLog.quota_date >= today - timedelta(days=6))
        summaries_30d = await count(AIQuotaLog, AIQuotaLog.quota_date >= today - timedelta(days=29))
        summaries_free = await count(
            AIQuotaLog,
            AIQuotaLog.from_user_key.is_(False),
            AIQuotaLog.from_credit.is_(False),
        )
        summaries_credit = await count(AIQuotaLog, AIQuotaLog.from_credit.is_(True))
        summaries_own_key = await count(AIQuotaLog, AIQuotaLog.from_user_key.is_(True))
        users_ai_active = int(
            (
                await db.execute(
                    select(func.count(func.distinct(AIQuotaLog.user_id)))
                )
            ).scalar_one()
            or 0
        )

        # 近 14 天摘要趋势
        summary_trend_rows = (
            await db.execute(
                select(AIQuotaLog.quota_date, func.count(AIQuotaLog.id))
                .where(AIQuotaLog.quota_date >= day_14)
                .group_by(AIQuotaLog.quota_date)
            )
        ).all()
        summary_trend = _day_counts(summary_trend_rows, 14, today)

        # 模型分布（TOP 8）
        model_rows = (
            await db.execute(
                select(AIQuotaLog.model, func.count(AIQuotaLog.id))
                .group_by(AIQuotaLog.model)
                .order_by(func.count(AIQuotaLog.id).desc())
                .limit(8)
            )
        ).all()
        model_usage = [
            {"model": model or "未记录", "count": int(n)} for model, n in model_rows
        ]

        # ---- 兑换码 ----
        batches_total = await count(RedeemBatch)
        codes_total = await count(RedeemCode)
        codes_used = await count(RedeemCode, RedeemCode.used_by.is_not(None))
        codes_unused = await count(
            RedeemCode,
            RedeemCode.used_by.is_(None),
            RedeemCode.revoked_at.is_(None),
        )
        codes_revoked = await count(
            RedeemCode, RedeemCode.revoked_at.is_not(None)
        )
        codes_used_today = await count(
            RedeemCode, RedeemCode.used_at >= today_start
        )
        # 面值合计（分），用已生成码的快照价——不受后续调价影响
        face_value_cents = int(
            (
                await db.execute(select(func.coalesce(func.sum(RedeemCode.price_cents), 0)))
            ).scalar_one()
            or 0
        )
        credits_issued = int(
            (
                await db.execute(select(func.coalesce(func.sum(RedeemCode.credits), 0)))
            ).scalar_one()
            or 0
        )
        credits_redeemed = int(
            (
                await db.execute(
                    select(func.coalesce(func.sum(RedeemCode.credits), 0)).where(
                        RedeemCode.used_by.is_not(None)
                    )
                )
            ).scalar_one()
            or 0
        )
        # 用户当前未花完的余额总和
        credits_outstanding = int(
            (
                await db.execute(
                    select(func.coalesce(func.sum(User.ai_summary_credits), 0))
                )
            ).scalar_one()
            or 0
        )

        # ---- 抓取健康 ----
        crawl_total_7d = await count(CrawlLog, CrawlLog.created_at >= week_ago)
        crawl_failed_7d = await count(
            CrawlLog, CrawlLog.created_at >= week_ago, CrawlLog.status == "failed"
        )
        crawl_today = await count(CrawlLog, CrawlLog.created_at >= today_start)
        crawl_success_rate = (
            round((1 - crawl_failed_7d / crawl_total_7d) * 100, 1)
            if crawl_total_7d
            else 100.0
        )

        # ---- 最近生成的兑换批次（TOP 5）----
        batch_rows = (
            await db.execute(
                select(RedeemBatch.batch_no, RedeemBatch.quantity, RedeemBatch.created_at)
                .order_by(RedeemBatch.created_at.desc())
                .limit(5)
            )
        ).all()

    summary_success_rate = (
        round(posts_summarized / posts_total * 100, 1) if posts_total else 0.0
    )

    return {
        "generated_at": now,
        "users": {
            "total": users_total,
            "active": users_active,
            "admin": users_admin,
            "new_today": users_new_today,
            "new_7d": users_new_7d,
            "with_sites": users_with_site,
        },
        "sites": {
            "total": sites_total,
            "active": sites_active,
            "paused": sites_paused,
            "error": sites_error,
            "link_pages": link_pages_total,
            "link_pages_active": link_pages_active,
        },
        "posts": {
            "total": posts_total,
            "today": posts_today,
            "last_7d": posts_7d,
            "summarized": posts_summarized,
            "pending": posts_pending,
            "failed": posts_failed,
            "edited": posts_edited,
            "summary_rate": summary_success_rate,
            "trend_14d": post_trend,
        },
        "ai": {
            "total": summaries_total,
            "today": summaries_today,
            "last_7d": summaries_7d,
            "last_30d": summaries_30d,
            "free": summaries_free,
            "from_credit": summaries_credit,
            "own_key": summaries_own_key,
            "active_users": users_ai_active,
            "trend_14d": summary_trend,
            "models": model_usage,
        },
        "redeem": {
            "batches": batches_total,
            "codes_total": codes_total,
            "used": codes_used,
            "unused": codes_unused,
            "revoked": codes_revoked,
            "used_today": codes_used_today,
            "face_value_yuan": round(face_value_cents / 100, 2),
            "credits_issued": credits_issued,
            "credits_redeemed": credits_redeemed,
            "credits_outstanding": credits_outstanding,
            "recent_batches": [
                {
                    "batch_no": no,
                    "quantity": int(qty),
                    "created_at": created,
                }
                for no, qty, created in batch_rows
            ],
        },
        "crawl": {
            "total_7d": crawl_total_7d,
            "failed_7d": crawl_failed_7d,
            "today": crawl_today,
            "success_rate": crawl_success_rate,
        },
    }
