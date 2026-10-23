"""AI 摘要服务。

计费双轨（需求 Q19=C）：
- 用户自带 Key → 不消耗平台额度，不受每日上限限制
- 用平台额度 → 按 (用户, 日期) 计数，超额直接拒绝

防重复花钱：正文哈希没变就不重新生成。这一条比什么优化都有效——
大多数文章抓取后正文不会再变。
"""

from __future__ import annotations

import logging
from datetime import date

import httpx
from sqlalchemy import func, select

from app.core.config import get_settings
from app.core.tenant import as_tenant_async
from app.db import session_scope
from app.models import AIQuotaLog, Post, Site, User
from app.services.extractor import (
    content_hash,
    extract_content,
    needs_summary,
)
from app.services.llm import LLMClient, LLMError, parse_summary_response

logger = logging.getLogger("ducircle.summary")


SUMMARY_PROMPT = """请阅读下面这篇博客文章，生成摘要和推荐分。

要求：
1. summary：用中文写2~3 句话概括核心内容，说清楚「作者讲了什么、有没有干货」。
   不要空话套话，不要"本文介绍了..."这种模板腔，直接讲内容。
2. score：0~10 的推荐分，衡量「值不值得点开」。
   9-10 必读干货；7-8 有价值；5-6 一般；3 以下可以不读。
3. reason：一句话说明打分理由。

文章标题：{title}
文章来源：{site}

正文：
{content}

只返回 JSON，格式：
{{"summary": "...", "score": 8.5, "reason": "..."}}"""

SYSTEM_PROMPT = (
    "你是一位资深博客编辑，擅长用简洁有力的中文概括文章要点。"
    "你的摘要直接讲内容，不使用任何模板化表达。"
)


class QuotaExceeded(Exception):
    """平台额度用尽。"""


async def check_quota(user: User, db) -> tuple[bool, int, int]:
    """检查额度。返回 (是否可用, 今日已用, 每日上限)。

    用户自带 Key 时永远可用，且不计入额度。
    """
    settings = get_settings()
    limit = user.quota_limit(settings.ai_daily_quota)
    if not user.ai_enabled:
        return False, 0, limit
    if user.has_own_ai_key:
        return True, 0, limit  # 自带 Key 不受限

    used = (
        await db.execute(
            select(func.count(AIQuotaLog.id)).where(
                AIQuotaLog.user_id == user.id,
                AIQuotaLog.quota_date == date.today(),
                AIQuotaLog.from_user_key.is_(False),
            )
        )
    ).scalar_one()

    return used < limit, used, limit


def _build_client(user: User, settings) -> tuple[LLMClient, str]:
    """构造客户端并标记是否消耗平台额度。"""
    if user.has_own_ai_key:
        return (
            LLMClient(
                provider=user.ai_provider or settings.ai_platform_provider,
                api_key=user.ai_api_key,
                model=user.ai_model,
            ),
            user.ai_provider or settings.ai_platform_provider,
        )
    return (
        LLMClient(
            provider=settings.ai_platform_provider,
            api_key=settings.ai_platform_api_key,
            model=settings.ai_platform_model,
        ),
        settings.ai_platform_provider,
    )


async def summarize_post(post: Post, user: User, db) -> dict | None:
    """为单篇文章生成摘要。

    返回 None 表示跳过（额度用尽、已存在、人工锁定等）。
    """
    settings = get_settings()
    if not settings.ai_enabled:
        logger.debug("AI 摘要功能未启用")
        return None

    # 人工修正过的摘要不覆盖——Q19 的需求里明确要这个保护
    if post.summary_edited:
        return None

    if not user.ai_enabled:
        return None

    # ---- 抽取正文 ----
    site_url = None
    site = await db.get(Site, post.site_id)
    if site:
        site_url = site.url

    content = await _fetch_and_extract(post.link, site_url)
    if not content:
        logger.info("文章 %d 正文抽取失败，跳过摘要", post.id)
        return None

    chash = content_hash(content)

    # 正文没变就不重复花钱
    if not needs_summary(post.summary_content_hash, chash):
        logger.debug("文章 %d 正文未变化，跳过", post.id)
        return None

    # ---- 额度检查 ----
    allowed, used, limit = await check_quota(user, db)
    if not allowed:
        raise QuotaExceeded(f"今日额度已用完（{used}/{limit}）")

    # ---- 调用模型 ----
    try:
        client, provider = _build_client(user, settings)
    except LLMError as exc:
        logger.warning("构造客户端失败：%s", exc)
        return None

    prompt = SUMMARY_PROMPT.format(
        title=post.title,
        site=site.name if site else "未知来源",
        content=content,
    )

    try:
        raw = await client.complete(
            prompt, system=SYSTEM_PROMPT, json_mode=True, max_tokens=600
        )
    except LLMError as exc:
        if exc.retryable:
            logger.warning("文章 %d 摘要失败（可重试）：%s", post.id, exc)
        else:
            logger.warning("文章 %d 摘要失败：%s", post.id, exc)
        return None

    summary, score, reason = parse_summary_response(raw)
    if not summary:
        return None

    # ---- 落库 ----
    post.summary = summary
    post.ai_score = score
    post.ai_reason = reason
    post.ai_model = client.model
    post.summary_content_hash = chash
    from datetime import datetime

    post.summary_at = datetime.now()

    # 记录额度消耗
    db.add(
        AIQuotaLog(
            user_id=user.id,
            post_id=post.id,
            quota_date=date.today(),
            provider=provider,
            model=client.model,
            from_user_key=user.has_own_ai_key,
        )
    )

    return {"summary": summary, "score": score, "model": client.model}


async def _fetch_and_extract(link: str, site_url: str | None) -> str | None:
    """抓取文章页面并抽取正文。"""
    settings = get_settings()
    try:
        async with httpx.AsyncClient(
            timeout=30,
            follow_redirects=True,
            headers={
                "User-Agent": settings.user_agent,
                "Accept-Language": "zh-CN,zh;q=0.9",
            },
        ) as client:
            resp = await client.get(link)
            if resp.status_code >= 400:
                return None
            return extract_content(resp.text, link)
    except (httpx.HTTPError, Exception) as exc:  # noqa: BLE001
        logger.debug("抓取正文失败 %s: %s", link, exc)
        return None


async def summarize_pending_for_user(user_id: int, limit: int = 20) -> dict:
    """为某用户批量生成待处理文章的摘要。

    优先级：没摘要的优先、正文可能更新的其次。
    """
    results = {"generated": 0, "skipped": 0, "quota_exceeded": False}

    async with session_scope() as db, as_tenant_async(user_id):
        user = await db.get(User, user_id)
        if user is None or not user.ai_enabled:
            return results

        posts = (
            await db.execute(
                select(Post)
                .where(
                    Post.summary.is_(None),
                    Post.summary_edited.is_(False),
                    Post.deleted_at.is_(None),
                )
                .order_by(Post.created.desc().nullslast())
                .limit(limit)
            )
        ).scalars().all()

        if not posts:
            return results

        for post in posts:
            try:
                result = await summarize_post(post, user, db)
                if result:
                    results["generated"] += 1
                else:
                    results["skipped"] += 1
            except QuotaExceeded as exc:
                logger.info("用户 %d 触发额度上限：%s", user_id, exc)
                results["quota_exceeded"] = True
                break

    return results


async def summarize_all_pending(limit_per_user: int = 10) -> dict:
    """定时任务：为所有用户生成待处理文章的摘要。"""
    from app.core.tenant import no_tenant_filter

    settings = get_settings()
    if not settings.ai_enabled or not settings.ai_platform_api_key:
        logger.info("平台未配置 AI，跳过摘要任务")
        return {"users": 0, "generated": 0, "quota_exceeded_users": 0}

    async with session_scope() as db, no_tenant_filter():
        users = (
            await db.execute(select(User.id).where(User.is_active.is_(True)))
        ).scalars().all()

    total_generated = 0
    quota_hit = 0

    for user_id in users:
        result = await summarize_pending_for_user(user_id, limit=limit_per_user)
        total_generated += result["generated"]
        if result["quota_exceeded"]:
            quota_hit += 1

    logger.info(
        "摘要任务完成：%d 用户，生成 %d 篇，%d 用户触发额度上限",
        len(users), total_generated, quota_hit,
    )
    return {
        "users": len(users),
        "generated": total_generated,
        "quota_exceeded_users": quota_hit,
    }


async def summarize_single(user_id: int, post_id: int) -> dict:
    """手动触发单篇摘要（后台的「生成摘要」按钮）。"""
    async with session_scope() as db, as_tenant_async(user_id):
        user = await db.get(User, user_id)
        post = await db.get(Post, post_id)
        if user is None or post is None:
            raise ValueError("用户或文章不存在")

        result = await summarize_post(post, user, db)
        if result is None:
            return {"ok": False, "reason": "无需生成（已存在或正文抽取失败）"}
        return {"ok": True, **result}