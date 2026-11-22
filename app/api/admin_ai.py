"""平台 AI 接口管理（仅管理员）。

平台模式的 API 地址 / Key / 模型从 .env 搬到数据库，管理员在后台改完
立即生效。三条约束：

1. 一律走 ``AdminUser`` 依赖——普通用户 403、未登录 401。
2. **Key 只回显脱敏形式**。明文只在提交时写入，GET 接口绝不返回完整值，
   避免管理页被截图/xss 时直接泄露服务端 Key。
3. 提交 Key 用「留空表示不修改」语义——管理员改模型时不必重新粘一遍 Key。
   真的要清空得显式传 ``clear_key: true``。
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.api.deps import AdminUser, DbSession
from app.core.config import get_settings
from app.models import AIProviderConfig
from app.models.ai_config import AI_CONFIG_SLOTS, SLOT_BACKUP, SLOT_PRIMARY
from app.services.llm import PROVIDERS, PROVIDER_DISPLAY_NAMES, LLMClient, LLMError

router = APIRouter(prefix="/api/admin/ai", tags=["平台 AI 设置"])
logger = logging.getLogger("ducircle.admin_ai")


def mask_key(key: str | None) -> str:
    """只留前 6 后 4 位，中间用星号——够管理员认出是哪把 Key。"""
    if not key:
        return ""
    if len(key) <= 10:
        return key[:2] + "***"
    return f"{key[:6]}***{key[-4:]}"


class ProviderOut(BaseModel):
    slot: str
    provider: str
    provider_label: str
    base_url: str
    model: str
    style: str
    key_masked: str
    has_key: bool
    enabled: bool
    updated_at: datetime | None = None


class ProvidersOut(BaseModel):
    items: list[ProviderOut]
    # db：库里至少一条启用配置，平台模式完全走库；env：一条都没启用，回退 .env
    source: str
    providers: list[dict]


class ProviderUpdateIn(BaseModel):
    provider: str = Field(default="", max_length=32)
    base_url: str = Field(default="", max_length=512)
    api_key: str = Field(default="", max_length=512)
    model: str = Field(default="", max_length=128)
    style: Literal["openai", "gemini"] = "openai"
    enabled: bool = False
    # 留空 api_key 表示保留原值；要清空必须显式给 true
    clear_key: bool = False


def _row_to_out(row: AIProviderConfig) -> ProviderOut:
    label = PROVIDER_DISPLAY_NAMES.get(row.provider or "", row.provider or "自定义接口")
    return ProviderOut(
        slot=row.slot,
        provider=row.provider or "",
        provider_label=label,
        base_url=row.base_url or "",
        model=row.model or "",
        style=row.style or "openai",
        key_masked=mask_key(row.api_key),
        has_key=bool(row.api_key),
        enabled=bool(row.enabled),
        updated_at=row.updated_at,
    )


def _empty_out(slot: str) -> ProviderOut:
    return ProviderOut(
        slot=slot,
        provider="",
        provider_label="自定义接口",
        base_url="",
        model="",
        style="openai",
        key_masked="",
        has_key=False,
        enabled=False,
        updated_at=None,
    )


@router.get("/providers", response_model=ProvidersOut, summary="查看主备 AI 接口配置")
async def get_providers(_admin: AdminUser, db: DbSession) -> ProvidersOut:
    rows = (await db.execute(select(AIProviderConfig))).scalars().all()
    by_slot = {r.slot: r for r in rows}
    items = [_row_to_out(by_slot[s]) if s in by_slot else _empty_out(s) for s in AI_CONFIG_SLOTS]
    source = "db" if any(i.enabled and i.has_key for i in items) else "env"
    return ProvidersOut(
        items=items,
        source=source,
        providers=[
            {
                "key": key,
                "label": PROVIDER_DISPLAY_NAMES.get(key, key),
                "base_url": cfg.base_url,
                "default_model": cfg.default_model,
            }
            for key, cfg in PROVIDERS.items()
        ],
    )


@router.put("/providers/{slot}", response_model=ProviderOut, summary="保存主/备 AI 接口")
async def update_provider(
    slot: str,
    payload: ProviderUpdateIn,
    admin: AdminUser,
    db: DbSession,
) -> ProviderOut:
    if slot not in AI_CONFIG_SLOTS:
        raise HTTPException(status_code=404, detail="只有 primary / backup 两个槽位")

    row = (
        await db.execute(select(AIProviderConfig).where(AIProviderConfig.slot == slot))
    ).scalar_one_or_none()
    if row is None:
        row = AIProviderConfig(slot=slot)
        db.add(row)

    provider = payload.provider.strip() or (row.provider if row else "") or ""
    if provider and provider not in PROVIDERS:
        raise HTTPException(status_code=400, detail=f"不支持的服务商：{provider}")

    # 留空表示保留原值：管理员常只改一个字段，不该被迫重填整张表。
    base_url = payload.base_url.strip().rstrip("/") or (row.base_url if row else "") or ""
    model = payload.model.strip() or (row.model if row else "") or ""
    style = payload.style or (row.style if row else "openai") or "openai"

    # 预置服务商的地址/模型仍为空时补默认值，管理员只填 Key 也能用。
    if provider:
        cfg = PROVIDERS[provider]
        if not base_url:
            base_url = cfg.base_url
        if not model:
            model = cfg.default_model
        if provider == "gemini":
            style = "gemini"

    # 启用时必须能构造出可调用的客户端，别保存一个点了没反应的配置。
    if payload.enabled:
        key = payload.api_key.strip() if payload.api_key.strip() else (row.api_key or "")
        if payload.clear_key:
            key = ""
        if not key and style != "ollama":
            raise HTTPException(status_code=400, detail="启用前必须填写 API Key（Ollama 本地模型除外）")
        if not base_url:
            raise HTTPException(status_code=400, detail="启用前必须填写 API 地址")
        if not model:
            raise HTTPException(status_code=400, detail="启用前必须填写模型名")
        try:
            LLMClient(provider, key, model, base_url=base_url, style=style)
        except LLMError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    row.provider = provider or None
    row.base_url = base_url or None
    row.model = model or None
    row.style = style
    row.enabled = payload.enabled
    if payload.clear_key:
        row.api_key = None
    elif payload.api_key.strip():
        row.api_key = payload.api_key.strip()
    # 否则保留原 Key

    await db.flush()
    logger.info("管理员 %s 更新平台 AI %s 配置（enabled=%s）", admin.username, slot, payload.enabled)
    return _row_to_out(row)


@router.post("/providers/{slot}/test", summary="测试主/备接口连通性")
async def test_provider(slot: str, _admin: AdminUser, db: DbSession) -> dict:
    """用保存的配置发一句最小请求，不扣任何用户额度。"""
    if slot not in AI_CONFIG_SLOTS:
        raise HTTPException(status_code=404, detail="只有 primary / backup 两个槽位")
    row = (
        await db.execute(select(AIProviderConfig).where(AIProviderConfig.slot == slot))
    ).scalar_one_or_none()
    if row is None or not row.enabled:
        raise HTTPException(status_code=400, detail="该接口未启用，请先保存并启用")

    try:
        client = LLMClient(
            row.provider or "",
            row.api_key or "",
            row.model,
            base_url=row.base_url,
            style=row.style,
        )
        raw = await client.complete("只回复两个字：正常", max_tokens=16, temperature=0)
    except LLMError as exc:
        return {"ok": False, "error": str(exc), "slot": slot}
    except Exception as exc:  # noqa: BLE001  测试接口要把底层错误如实带回
        logger.warning("测试平台 AI %s 失败：%s", slot, exc)
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:300], "slot": slot}

    return {"ok": True, "reply": raw[:100], "slot": slot, "model": client.model}


@router.get("/source", summary="当前平台 AI 配置来源")
async def get_source(_admin: AdminUser, db: DbSession) -> dict:
    rows = (await db.execute(select(AIProviderConfig))).scalars().all()
    enabled = [r for r in rows if r.enabled and r.api_key]
    settings = get_settings()
    return {
        "source": "db" if enabled else "env",
        "slots": [r.slot for r in enabled],
        "env_provider": settings.ai_platform_provider,
        "env_model": settings.ai_platform_model,
        "env_has_key": bool(settings.ai_platform_api_key),
    }
