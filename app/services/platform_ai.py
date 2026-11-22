"""平台 AI 配置：读库、主备切换、回退 .env。

设计要点：

1. **库里没配置就回退 .env**——升级前后行为完全一致，
   线上现存的 AI_PLATFORM_* 不用迁移也不会突然失效。
2. **只要有一条启用的槽位配置，就完全接管平台模式**——此时不再读 .env。
   避免出现「库里改了 Key 但还在用 .env 那个」这种让人抓狂的半生效状态。
3. **主备自动切换只在可恢复错误上做**：超时 / 网络 / 5xx / 429 / 401
   都算；内容安全拦截这类 permanent 错误不切，切了也是白花钱。
4. **自定义 AI 模式不碰这里**——用户自带 Key 走自己的服务商。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import select

from app.core.config import get_settings
from app.models import AIProviderConfig
from app.models.ai_config import SLOT_BACKUP, SLOT_PRIMARY
from app.services.llm import LLMClient, LLMError

logger = logging.getLogger("ducircle.platform_ai")


@dataclass
class PlatformClient:
    """一个可用的平台 AI 客户端 + 它的展示信息。"""

    client: LLMClient
    slot: str
    label: str


async def load_platform_clients(db) -> tuple[list[PlatformClient], str]:
    """返回 (按主→备排序的客户端列表, 配置来源)。

    来源取值：
    - ``db``：库里至少有一个启用的槽位，完全用库里的配置
    - ``env``：库里一条启用配置都没有，回退 .env（升级兼容）
    """
    settings = get_settings()
    rows = (
        (await db.execute(select(AIProviderConfig))).scalars().all()
    )
    by_slot = {r.slot: r for r in rows}

    clients: list[PlatformClient] = []
    for slot, default_label in ((SLOT_PRIMARY, "主 API"), (SLOT_BACKUP, "备 API")):
        row = by_slot.get(slot)
        if row is None or not row.enabled:
            continue
        if not row.api_key and (row.style or "openai") != "ollama":
            # Ollama 本地模型可以无 Key；其余服务商空 Key 会在
            # LLMClient.complete() 里报「Bearer 」非法头，提前跳过更清楚。
            logger.warning("平台 AI %s 未填 Key，跳过", slot)
            continue
        try:
            client = LLMClient(
                provider=row.provider or "",
                api_key=row.api_key or "",
                model=row.model,
                base_url=row.base_url,
                style=row.style,
            )
        except LLMError as exc:
            logger.warning("平台 AI %s 配置无效，跳过：%s", slot, exc)
            continue
        clients.append(PlatformClient(client, slot, default_label))

    if clients:
        return clients, "db"

    # 回退 .env：保持与改造前完全一致
    try:
        client = LLMClient(
            provider=settings.ai_platform_provider,
            api_key=settings.ai_platform_api_key,
            model=settings.ai_platform_model,
        )
    except LLMError as exc:
        # Key 为空时 LLMClient 构造仍成功，complete() 才报错——
        # 那里有给管理员看的明确提示，这里不要吞掉。
        logger.debug("平台 AI .env 配置未就绪：%s", exc)
        return [], "env"
    return [PlatformClient(client, SLOT_PRIMARY, "主 API（.env）")], "env"


async def complete_on_platform(
    clients: list[PlatformClient], prompt: str, **kwargs
) -> tuple[str, PlatformClient]:
    """依次尝试主备，返回第一个成功的结果和它用的客户端。

    全部失败时抛最后一个异常——调用方拿到的错误信息是真正
    最后一次尝试的原因，而不是被覆盖前的第一条。
    """
    last_exc: LLMError | None = None
    for item in clients:
        try:
            raw = await item.client.complete(prompt, **kwargs)
            return raw, item
        except LLMError as exc:
            last_exc = exc
            logger.warning("平台 AI %s 调用失败：%s", item.slot, exc)
            # 内容安全拦截等永久错误：换一家也不会变，别浪费额度
            if getattr(exc, "permanent", True) and not exc.retryable:
                break
            continue
    if last_exc is None:
        raise LLMError("平台 AI 未配置可用的接口，请联系管理员")
    raise last_exc
