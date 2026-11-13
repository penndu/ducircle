"""用户设置与数据管理接口。"""

from __future__ import annotations

import time
from typing import Annotated

from fastapi import APIRouter, Body, HTTPException, Query, Response
from pydantic import BaseModel, Field
from sqlalchemy import delete, select

from app.api.deps import CurrentUserSession, DbSession
from app.core.security import generate_api_key, hash_api_key, verify_api_key
from app.core.session import SESSION_COOKIE
from app.models import CrawlLog, Post, Site, SiteRule

router = APIRouter(prefix="/api/users", tags=["用户设置"])


class PasswordChange(BaseModel):
    old_password: str = Field(min_length=1, max_length=128)
    # 长度下限跟注册保持一致（8~128）
    new_password: str = Field(min_length=8, max_length=128)


class UserSettingsUpdate(BaseModel):
    ai_enabled: bool | None = None
    ai_provider: str | None = None
    ai_model: str | None = None
    ai_api_key: str | None = None
    max_concurrent_override: int | None = None
    ai_daily_quota_override: int | None = None
    # 采集频率（cron 表达式）。空字符串表示用平台默认。
    crawl_cron: str | None = None
    link_page_cron: str | None = None


class ApiKeyImport(BaseModel):
    # 只做格式下限校验，真正的判断是 sha256 是否等于库里的哈希。
    api_key: str = Field(min_length=8, max_length=128)


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

    # cron 表达式必须先校验。
    # 非法表达式进库后，调度器在后台跑起来才炸，
    # 用户完全感知不到——所以宁可现在拒绝保存。
    from app.core.cron import validate_cron

    for field, label in (
        ("crawl_cron", "文章采集频率"),
        ("link_page_cron", "友链采集频率"),
    ):
        if field not in data:
            continue
        value = data[field]
        # 空字符串等价于「用平台默认」
        if value is not None and not value.strip():
            data[field] = None
            continue
        result = validate_cron(value)
        if not result.valid:
            raise HTTPException(
                status_code=400,
                detail=f"{label}无效：{result.error}",
            )

    for field, value in data.items():
        setattr(user, field, value)

    await db.flush()
    return {
        "ok": True,
        "ai_enabled": user.ai_enabled,
        "ai_provider": user.ai_provider,
        "has_own_ai_key": user.has_own_ai_key,
        "crawl_cron": user.crawl_cron,
        "link_page_cron": user.link_page_cron,
    }


@router.post("/me/password", summary="修改密码")
async def change_password(
    payload: PasswordChange,
    user: CurrentUserSession,
    db: DbSession,
    response: Response,
) -> dict:
    """修改登录密码。

    **必须验证原密码**——只凭已登录就能改密的话，
    别人拿到你的 Cookie（比如 XSS 拿到）就能永久夺走账号。

    改密后：
    - 所有旧会话立即失效（靠 password_changed_at 与签发时间比较）
    - 当前会话换发新的 Cookie，不受影响
    """
    from app.core.security import hash_password, verify_password

    # 原密码不对 → 明确说「原密码错误」，不泄露任何其它信息
    if not verify_password(payload.old_password, user.password_hash):
        raise HTTPException(status_code=400, detail="原密码不正确")

    # 新旧密码相同没意义，但也不拦——用户可能就是想「确认」一下
    if payload.old_password == payload.new_password:
        raise HTTPException(status_code=400, detail="新密码不能与原密码相同")

    user.password_hash = hash_password(payload.new_password)
    # 记下改密时刻（Unix 时间戳）：所有在它之前签发的会话都作废
    changed_at = int(time.time())
    user.password_changed_at = changed_at

    await db.flush()
    await db.commit()

    # 换发新会话：当前浏览器继续可用，但用的是改密之后签发的。
    # 签发时间必须**严格大于**改密时刻——校验逻辑要求 issued_at > changed_at，
    # 同一秒签发的新会话会被当成旧会话挡掉。
    from app.core.session import session_cookie_kwargs, write_session

    response.set_cookie(
        SESSION_COOKIE,
        write_session(user.id, issued_at=changed_at + 1),
        **session_cookie_kwargs(),
    )

    return {"ok": True, "message": "密码已修改，其它设备的登录状态已失效"}


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
    # 明文副本同步更新，否则设置页又会退回「（暂无）」
    user.api_key_plain = key
    await db.flush()
    return {"api_key": key}


@router.post("/me/api-key/import", summary="认领已有 API Key")
async def import_api_key(
    payload: ApiKeyImport, user: CurrentUserSession, db: DbSession
) -> dict:
    """把正在使用的 Key 认领进账号，之后设置页就能一直看到它。

    只为存量账号存在：v2.0.1 之前只存哈希，明文无从还原，
    于是设置页永远显示「（暂无）」——用户会误以为 Key 丢了，
    转而去「重新生成」，而重新生成必然换掉 Key。

    这里**只写入明文副本，不改 api_key_hash**：
    Key 本身一个字符都不变，鉴权结果自然也不变，
    博客前端不需要改配置。

    判断依据是 sha256 是否等于库里的哈希——相等才说明
    这确实是本人当前使用的那个 Key，别人拿到别人的 Key 也认领不了。
    """
    key = payload.api_key.strip()
    if not user.api_key_hash or not verify_api_key(key, user.api_key_hash):
        raise HTTPException(
            status_code=400, detail="这个 Key 与当前账号不匹配"
        )

    user.api_key_plain = key
    await db.flush()
    return {"ok": True, "api_key": key}

@router.get("/me/cron-presets", summary="采集频率预设")
async def cron_presets(user: CurrentUserSession) -> dict:
    """返回常用频率预设。

    前端用它渲染下拉框——让用户选而不是手写 cron 表达式，
    大部分人不知道 cron 怎么写。
    """
    from app.core.cron import CRON_PRESETS

    return {
        "presets": [
            {"label": label, "value": value, "hint": hint}
            for label, value, hint in CRON_PRESETS
        ]
    }
