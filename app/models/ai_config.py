"""平台 AI 接口配置。

管理员在后台填的 API 地址 / Key / 模型落在这里，不再要求改 .env 重启容器。
两条记录：primary（主）+ backup（备）。主失败时自动切备。

为什么不用「配置表一行存 JSON」：
两行固定槽位在页面上是两张独立表单，字段一致、校验一致，
列表查询也简单。JSON 方案省一行表，却要为 dict 缺失字段写一堆兜底。
"""

from __future__ import annotations

from sqlalchemy import Boolean, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin

SLOT_PRIMARY = "primary"
SLOT_BACKUP = "backup"
AI_CONFIG_SLOTS = (SLOT_PRIMARY, SLOT_BACKUP)


class AIProviderConfig(Base, TimestampMixin):
    __tablename__ = "ai_provider_configs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # primary / backup，唯一且固定两行
    slot: Mapped[str] = mapped_column(String(16), unique=True, nullable=False, index=True)
    # 预置服务商标识（siliconflow / minimax_cn / ...）。
    # 自定义 OpenAI 兼容接口留空，只填 base_url。
    provider: Mapped[str | None] = mapped_column(String(32), default=None)
    # OpenAI 兼容接口填到 /v1 即可，Gemini 填到 /v1beta。
    # 留空表示用 provider 的预置地址。
    base_url: Mapped[str | None] = mapped_column(String(512), default=None)
    # 明文存储，与 users.ai_api_key 的取舍一致：能查看/复制，
    # 但接口回显必须脱敏（见 admin_ai.mask_key）。
    api_key: Mapped[str | None] = mapped_column(Text, default=None)
    model: Mapped[str | None] = mapped_column(String(128), default=None)
    # openai | gemini。自定义接口默认 openai。
    style: Mapped[str] = mapped_column(
        String(16), default="openai", nullable=False, server_default="openai"
    )
    enabled: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False, server_default="0"
    )
