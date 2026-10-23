"""LLM 客户端。

支持两类服务商：
- OpenAI 兼容（SiliconFlow / DeepSeek / Moonshot / 本地 Ollama / 任何兼容的）
- Gemini（Google 的 API 格式不同，单独处理）

抽象成一个统一接口，新增服务商只需加一条配置。
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass

import httpx

from app.core.config import get_settings

logger = logging.getLogger("fcircle.llm")


class LLMError(Exception):
    """LLM 调用失败。区分可重试与不可重试，便于降级处理。"""

    def __init__(self, message: str, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


@dataclass
class ProviderConfig:
    """服务商配置。"""

    name: str
    base_url: str
    default_model: str
    # Gemini 用不同的鉴权与请求格式
    style: str = "openai"  # openai | gemini


PROVIDERS: dict[str, ProviderConfig] = {
    "siliconflow": ProviderConfig(
        name="siliconflow",
        base_url="https://api.siliconflow.cn/v1",
        default_model="Qwen/Qwen3-8B",
    ),
    "deepseek": ProviderConfig(
        name="deepseek",
        base_url="https://api.deepseek.com/v1",
        default_model="deepseek-chat",
    ),
    "moonshot": ProviderConfig(
        name="moonshot",
        base_url="https://api.moonshot.cn/v1",
        default_model="moonshot-v1-8k",
    ),
    "gemini": ProviderConfig(
        name="gemini",
        base_url="https://generativelanguage.googleapis.com/v1beta",
        default_model="gemini-2.0-flash",
        style="gemini",
    ),
    # 本地 Ollama，兼容 OpenAI 协议
    "ollama": ProviderConfig(
        name="ollama",
        base_url="http://localhost:11434/v1",
        default_model="qwen2.5:7b",
    ),
}


@dataclass
class SummaryResult:
    """摘要生成结果。"""

    summary: str
    score: float | None
    reason: str | None
    model: str
    provider: str


class LLMClient:
    """统一的 LLM 调用入口。"""

    def __init__(self, provider: str, api_key: str, model: str | None = None) -> None:
        config = PROVIDERS.get(provider)
        if config is None:
            raise LLMError(f"不支持的服务商：{provider}")

        self.config = config
        self.api_key = api_key
        self.model = model or config.default_model

    async def complete(
        self,
        prompt: str,
        *,
        system: str | None = None,
        max_tokens: int = 800,
        temperature: float = 0.3,
        json_mode: bool = False,
    ) -> str:
        settings = get_settings()

        headers = {"Content-Type": "application/json"}
        if self.config.style == "gemini":
            url = f"{self.config.base_url}/models/{self.model}:generateContent"
            headers["x-goog-api-key"] = self.api_key
        else:
            url = f"{self.config.base_url}/chat/completions"
            headers["Authorization"] = f"Bearer {self.api_key}"

        if self.config.style == "gemini":
            payload: dict = {
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {
                    "maxOutputTokens": max_tokens,
                    "temperature": temperature,
                },
            }
            if system:
                payload["systemInstruction"] = {"parts": [{"text": system}]}
            if json_mode:
                payload["generationConfig"]["responseMimeType"] = "application/json"
        else:
            messages = []
            if system:
                messages.append({"role": "system", "content": system})
            messages.append({"role": "user", "content": prompt})
            payload = {
                "model": self.model,
                "messages": messages,
                "max_tokens": max_tokens,
                "temperature": temperature,
                "stream": False,
            }
            if json_mode:
                payload["response_format"] = {"type": "json_object"}

        try:
            async with httpx.AsyncClient(timeout=settings.ai_timeout) as client:
                resp = await client.post(url, headers=headers, json=payload)

                if resp.status_code == 401:
                    raise LLMError("API Key 无效", retryable=False)
                if resp.status_code == 429:
                    raise LLMError("触发服务商限流", retryable=True)
                if resp.status_code >= 500:
                    raise LLMError(f"服务商错误 {resp.status_code}", retryable=True)
                if resp.status_code >= 400:
                    detail = resp.text[:200]
                    raise LLMError(f"请求被拒 {resp.status_code}: {detail}", retryable=False)

                data = resp.json()
        except httpx.TimeoutException as exc:
            raise LLMError(f"请求超时：{exc}", retryable=True) from exc
        except httpx.TransportError as exc:
            raise LLMError(f"网络错误：{exc}", retryable=True) from exc

        return self._extract_text(data)

    def _extract_text(self, data: dict) -> str:
        if self.config.style == "gemini":
            try:
                candidates = data.get("candidates") or []
                if not candidates:
                    raise LLMError("返回内容为空")
                parts = candidates[0].get("content", {}).get("parts", [])
                return "".join(p.get("text", "") for p in parts).strip()
            except (KeyError, IndexError, TypeError) as exc:
                raise LLMError(f"解析 Gemini 响应失败：{data}") from exc

        try:
            choices = data.get("choices") or []
            if not choices:
                raise LLMError("返回内容为空")
            return (choices[0].get("message", {}).get("content") or "").strip()
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError(f"解析响应失败：{json.dumps(data)[:200]}") from exc


def parse_summary_response(text: str) -> tuple[str, float | None, str | None]:
    """解析模型返回的 JSON。

    模型即使被要求返回 JSON 也可能包markdown 代码块或说些废话，
    这里做防御性解析，任何异常都退化为「纯文本摘要」。
    """
    cleaned = text.strip()

    # 剥掉 ```json ... ``` 包裹
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    cleaned = cleaned.strip()

    try:
        data = json.loads(cleaned)
        if not isinstance(data, dict):
            raise ValueError("不是对象")

        summary = str(data.get("summary", "")).strip()
        if not summary:
            # 返回空串而不是抛错：空摘要等于「没生成出来」，
            # 调用方会跳过。若抛错被上层 catch，会退化成把原始 JSON
            # 当摘要存进库——那比没有摘要更糟。
            return "", None, None

        score = data.get("score")
        try:
            score = float(score) if score is not None else None
            if score is not None:
                score = max(0.0, min(10.0, score))  # 防止模型给出离谱的值
        except (TypeError, ValueError):
            score = None

        reason = data.get("reason")
        return summary, score, (str(reason).strip() if reason else None)

    except (json.JSONDecodeError, ValueError, TypeError):
        # 模型没按要求返回 JSON，直接把文本当摘要用
        return cleaned[:500], None, None


import re  # noqa: E402  （放在末尾避免与上方 logger 定义冲突时的可读性问题）