"""AI 摘要服务。

计费双轨（需求 Q19=C）：
- 用户自带 Key → 不消耗平台额度，不受每日上限限制
- 用平台额度 → 按 (用户, 日期) 计数，超额直接拒绝

**平台额度其实是两桶**（v2.1.0 起）：

- 免费日额度 ``ai_daily_quota``：平台送的，每天重置，不用就作废
- 兑换余额 ``users.ai_summary_credits``：用户用兑换码买的，可累加、不过期

先花免费的那桶，再花花钱买的——免费额度当天不作废就没了，
余额永远在，顺序反了用户会觉得自己被薅。

防重复花钱：正文哈希没变就不重新生成。这一条比什么优化都有效——
大多数文章抓取后正文不会再变。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
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
from app.services.summary_status import set_status
from app.core.sql_compat import desc_nulls_last

logger = logging.getLogger("ducircle.summary")


SUMMARY_PROMPT = """请阅读下面这篇博客文章，生成摘要和推荐分。

要求：
1. summary：用中文写2~3 句话概括核心内容，说清楚「作者讲了什么、有没有干货」。
   不要空话套话，不要"本文介绍了..."这种模板腔，直接讲内容。
   **控制在 150 字以内**，别展开细说。
2. score：0~10 的推荐分，衡量「值不值得点开」。
   9-10 必读干货；7-8 有价值；5-6 一般；3 以下可以不读。
3. reason：一句话说明打分理由，30 字以内。

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


class AIKeyRequired(Exception):
    """用户没填自己的 API Key，而平台又不提供免费额度。

    与 QuotaExceeded 分开是因为提示完全不同：
    前者是「今天用完了，明天再来」，
    后者是「填个 Key 就能用」——后者是可操作的引导。
    """


async def check_quota(user: User, db) -> tuple[bool, int, int]:
    """兼容旧调用：返回 (是否可用, 今日已用免费额度, 每日免费上限)。"""
    state = await quota_state(user, db)
    return state.allowed, state.used_today, state.daily_limit


@dataclass
class QuotaState:
    """一次额度判定的完整结果。

    ``source`` 决定这次生成从哪一桶里扣：

    - ``own_key``：用户自己的 Key，不占平台任何资源
    - ``free``：平台的每日免费额度
    - ``credit``：兑换码换来的余额（用户付过钱）
    - ``none``：都不行——要么去填 Key，要么去兑一张码
    """

    source: str
    used_today: int
    daily_limit: int
    credits: int

    @property
    def allowed(self) -> bool:
        return self.source != "none"

    @property
    def remaining(self) -> int | None:
        """还能生成几次。None 表示不受限（自带 Key）。"""
        if self.source == "own_key":
            return None
        return max(0, self.daily_limit - self.used_today) + self.credits


async def quota_state(user: User, db) -> QuotaState:
    """判定这次生成能用哪一桶额度。

    优先级：**自带 Key > 每日免费额度 > 兑换余额**。

    免费额度排在付费余额前面，是因为免费额度当天不用就作废，
    而余额永远在——先花会过期的那个，对用户最有利。
    """
    settings = get_settings()
    limit = user.quota_limit(settings.ai_daily_quota)
    credits = user.ai_summary_credits or 0

    if not user.ai_enabled:
        return QuotaState("none", 0, limit, credits)
    if user.has_own_ai_key:
        return QuotaState("own_key", 0, limit, credits)

    # 只数「免费额度」那一桶：带 from_user_key 的是用户自己花的钱，
    # 带 from_credit 的是用户买来的余额，都不能算进平台白送的额度。
    used = (
        await db.execute(
            select(func.count(AIQuotaLog.id)).where(
                AIQuotaLog.user_id == user.id,
                AIQuotaLog.quota_date == date.today(),
                AIQuotaLog.from_user_key.is_(False),
                AIQuotaLog.from_credit.is_(False),
            )
        )
    ).scalar_one()

    used = int(used)
    if limit > 0 and used < limit:
        return QuotaState("free", used, limit, credits)
    if credits > 0:
        return QuotaState("credit", used, limit, credits)
    return QuotaState("none", used, limit, credits)


async def _log_usage(
    db,
    *,
    user: User,
    post: Post,
    provider: str,
    model: str | None,
    from_user_key: bool,
    from_credit: bool,
) -> None:
    """记一笔额度消耗。

    ``(user_id, quota_date, post_id)`` 上有唯一约束：用户点了「重置摘要」
    再生成一次，第二次插入会撞约束、把这批任务的会话搞脏。
    所以先查一次，有就不重复写——这一行的用途是「今日用量统计」，
    不是账本；真正的账本是 ``users.ai_summary_credits`` 余额。
    """
    today = date.today()
    exists = (
        await db.execute(
            select(AIQuotaLog.id).where(
                AIQuotaLog.user_id == user.id,
                AIQuotaLog.quota_date == today,
                AIQuotaLog.post_id == post.id,
            )
        )
    ).first()
    if exists:
        return

    db.add(
        AIQuotaLog(
            user_id=user.id,
            post_id=post.id,
            quota_date=today,
            provider=provider,
            model=model,
            from_user_key=from_user_key,
            from_credit=from_credit,
        )
    )


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


async def summarize_post(
    post: Post, user: User, db, *, skip_reason: list[str] | None = None
) -> dict | None:
    """为单篇文章生成摘要。

    返回 None 表示跳过（正文抽取失败、人工锁定等）。
    skip_reason 传入列表时，跳过原因写进列表——手动触发的按钮需要把它提示给用户。
    """

    def skip(reason: str) -> None:
        if skip_reason is not None:
            skip_reason.append(reason)
        return None

    settings = get_settings()
    if not settings.ai_enabled:
        logger.debug("AI 摘要功能未启用")
        return skip("AI 摘要功能未启用")

    # 人工修正过的摘要不覆盖——Q19 的需求里明确要这个保护
    if post.summary_edited:
        return skip("这篇摘要已人工修改，不会被覆盖")

    if not user.ai_enabled:
        return skip("你的 AI 摘要已停用，请到「设置 → AI 摘要」开启")

    # ---- 抽取正文 ----
    site_url = None
    site = await db.get(Site, post.site_id)
    if site:
        site_url = site.url

    content = await _extract_content(post, site_url)
    if not content:
        logger.info("文章 %d 正文抽取失败，跳过摘要", post.id)
        return skip("正文抽取失败：文章页可能是动态渲染或已失效")

    chash = content_hash(content)

    # 正文没变就不重复花钱
    if not needs_summary(post.summary_content_hash, chash):
        logger.debug("文章 %d 正文未变化，跳过", post.id)
        return skip("正文未变化，无需重新生成")

    # ---- 额度检查 ----
    # state 后面还要用来判断「这次从哪一桶扣」，所以整体留着而不是只取 allowed
    state = await quota_state(user, db)
    if not state.allowed:
        if state.daily_limit <= 0:
            # 平台不提供免费额度时，两条路：填自己的 Key，或兑一张码
            raise AIKeyRequired(
                "生成摘要需要你自己的 API Key，或用兑换码兑换平台内置 AI 的摘要次数。"
                "到「设置 → AI 摘要」填写或兑换，填完立即生效。"
            )
        raise QuotaExceeded(
            f"今日额度已用完（{state.used_today}/{state.daily_limit}）"
        )

    # ---- 调用模型 ----
    try:
        client, provider = _build_client(user, settings)
    except LLMError as exc:
        logger.warning("构造客户端失败：%s", exc)
        return skip(str(exc))

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
        elif not getattr(exc, "permanent", True):
            # Key 无效这类：现在不该重试，但换 Key 后能恢复。
            # 不能标记 summary_failed，否则批量生成永远跳过这篇。
            logger.warning("文章 %d 摘要失败（换 Key 后可恢复）：%s", post.id, exc)
        else:
            # 真正不可重试（如模型内容安全拦截）——再试多少次都一样。
            # 标记出来，以后批量生成直接跳过，不浪费时间和额度。
            logger.warning("文章 %d 摘要失败（不再重试）：%s", post.id, exc)
            post.summary_failed = True
            post.summary_error = str(exc)[:500]
        return skip(str(exc))

    summary, score, reason = parse_summary_response(raw)
    if not summary:
        return skip("模型返回内容无法解析为摘要，请重试")

    # ---- 落库 ----
    post.summary = summary
    post.ai_score = score
    post.ai_reason = reason
    post.ai_model = client.model
    post.summary_content_hash = chash
    from datetime import datetime

    post.summary_at = datetime.now()

    # ---- 扣额度 ----
    # 兑换余额是用户付过钱的东西，必须真的扣在这个字段上；
    # 免费额度和自带 Key 只记一笔日志，不动余额。
    from_credit = state.source == "credit"
    if from_credit:
        user.ai_summary_credits = max(0, (user.ai_summary_credits or 0) - 1)

    await _log_usage(
        db,
        user=user,
        post=post,
        provider=provider,
        model=client.model,
        from_user_key=user.has_own_ai_key,
        from_credit=from_credit,
    )

    return {"summary": summary, "score": score, "model": client.model}


async def _extract_content(post, site_url: str | None) -> str | None:
    """拿到文章正文。

    两条路：
    1. **feed 里存的全文**——抓文章时从 RSS 的 content:encoded 顺手存下的。
       对「文章页是 SPA 骨架页」的站点这是唯一可行的途径：
       那些站返回的 HTML 只有一两 KB，正文全靠 JS 渲染，静态抓取拿不到。
    2. **抓文章页再抽取**——常规站点走这条，正文更完整（含更新后的内容）。

    优先用 1：它是现成的，不用再发请求，速度快一个数量级。
    抽不出（feed 没带全文、或者内容不像正文）再退到 2。
    """
    # 第 1 条：feed 全文
    raw = getattr(post, "raw_content", None)
    if raw:
        text = extract_content(raw, post.link)
        if text and len(text) >= 120:
            return text

    # 第 2 条：抓文章页
    return await _fetch_and_extract(post.link, site_url)


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

    过程中会把进度写进 Redis（summary_status），
    前端轮询这个状态就能看到「跑到第几篇、失败没、为什么失败」——
    后台任务的异常会被 except 吞掉，不主动记状态的话用户只能干等。
    """
    results = {"generated": 0, "skipped": 0, "quota_exceeded": False,
               "key_required": False, "error": None}

    await set_status(
        user_id,
        {"state": "running", "generated": 0, "skipped": 0,
         "total": limit, "error": None},
    )

    async with session_scope() as db, as_tenant_async(user_id):
        user = await db.get(User, user_id)
        if user is None or not user.ai_enabled:
            await set_status(
                user_id,
                {"state": "failed", "generated": 0, "skipped": 0,
                 "total": 0, "error": "AI 摘要功能未启用"},
            )
            return results

        posts = (
            await db.execute(
                select(Post)
                .where(
                    Post.summary.is_(None),
                    Post.summary_edited.is_(False),
                    Post.deleted_at.is_(None),
                    # 永久失败的文章不再重试（模型内容安全拦截等），
                    # 否则每轮批量生成都会重新选中它，白费额度
                    Post.summary_failed.is_(False),
                )
                .order_by(desc_nulls_last(Post.created))
                .limit(limit)
            )
        ).scalars().all()

        if not posts:
            await set_status(
                user_id,
                {"state": "done", "generated": 0, "skipped": 0,
                 "total": 0, "error": None},
            )
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
                results["error"] = str(exc)
                break
            except AIKeyRequired as exc:
                # 用户没填自己的 Key，生成不了。
                # 不是错误，是配置缺失——记一笔就跳过，继续处理下一个用户。
                logger.info("用户 %d 未配置 API Key，跳过摘要", user_id)
                results["key_required"] = True
                results["error"] = str(exc)
                break
            except Exception as exc:  # noqa: BLE001
                # 单篇失败不该让整批中断，但要让用户看到发生了什么
                logger.exception("用户 %d 生成摘要时出错", user_id)
                results["error"] = f"{type(exc).__name__}: {exc}"
                break

            # 每篇都更新一次进度，用户轮询能看到在动
            await set_status(
                user_id,
                {"state": "running", "generated": results["generated"],
                 "skipped": results["skipped"], "total": len(posts),
                 "error": None},
            )

    # 收尾写终态
    if results["error"] and not results["key_required"]:
        state = "failed"
    else:
        state = "done"
    await set_status(
        user_id,
        {"state": state, "generated": results["generated"],
         "skipped": results["skipped"], "total": len(posts),
         "error": results["error"]},
    )

    return results


async def summarize_all_pending(limit_per_user: int = 10) -> dict:
    """定时任务：为所有用户生成待处理文章的摘要。"""
    from app.core.tenant import no_tenant_filter_async

    settings = get_settings()
    if not settings.ai_enabled:
        logger.info("AI 摘要功能未启用，跳过摘要任务")
        return {
            "users": 0,
            "generated": 0,
            "quota_exceeded_users": 0,
            "key_required_users": 0,
        }

    async with session_scope() as db, no_tenant_filter_async():
        users = (
            await db.execute(select(User.id).where(User.is_active.is_(True)))
        ).scalars().all()

    total_generated = 0
    quota_hit = 0
    key_missing = 0

    for user_id in users:
        result = await summarize_pending_for_user(user_id, limit=limit_per_user)
        total_generated += result["generated"]
        if result["quota_exceeded"]:
            quota_hit += 1
        if result.get("key_required"):
            key_missing += 1

    logger.info(
        "摘要任务完成：%d 用户，生成 %d 篇，%d 用户触发额度上限，%d 用户未配置 Key",
        len(users), total_generated, quota_hit, key_missing,
    )
    return {
        "users": len(users),
        "generated": total_generated,
        "quota_exceeded_users": quota_hit,
        "key_required_users": key_missing,
    }


async def summarize_single(user_id: int, post_id: int) -> dict:
    """手动触发单篇摘要（后台的「生成摘要」按钮）。"""
    async with session_scope() as db, as_tenant_async(user_id):
        user = await db.get(User, user_id)
        post = await db.get(Post, post_id)
        if user is None or post is None:
            raise ValueError("用户或文章不存在")

        reasons: list[str] = []
        result = await summarize_post(post, user, db, skip_reason=reasons)
        if result is None:
            return {
                "ok": False,
                "reason": reasons[0] if reasons else "无需生成（正文抽取失败或已存在）",
            }
        return {"ok": True, **result}