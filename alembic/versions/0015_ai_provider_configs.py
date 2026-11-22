"""平台 AI 接口配置：主 API + 备 API

管理员从后台页面填 API 地址 / Key / 模型，不用再改 .env 重启容器。
primary / backup 两个固定槽位：主失败自动切备。

表里**不预插数据**：空表表示「沿用 .env 里的 AI_PLATFORM_* 配置」，
升级前后行为完全一致。管理员在页面上保存后才开始用库里的配置。

Revision ID: 0015_ai_provider_configs
Revises: 0014_user_summary_mode
Create Date: 2026-11-22
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0015_ai_provider_configs"
down_revision: str | None = "0014_user_summary_mode"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "ai_provider_configs",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("slot", sa.String(length=16), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=True),
        sa.Column("base_url", sa.String(length=512), nullable=True),
        sa.Column("api_key", sa.Text(), nullable=True),
        sa.Column("model", sa.String(length=128), nullable=True),
        sa.Column(
            "style",
            sa.String(length=16),
            server_default="openai",
            nullable=False,
        ),
        sa.Column(
            "enabled",
            sa.Boolean(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("slot"),
    )
    op.create_index(
        op.f("ix_ai_provider_configs_slot"), "ai_provider_configs", ["slot"], unique=True
    )
    op.create_index(
        op.f("ix_ai_provider_configs_created_at"),
        "ai_provider_configs",
        ["created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_ai_provider_configs_created_at"), table_name="ai_provider_configs")
    op.drop_index(op.f("ix_ai_provider_configs_slot"), table_name="ai_provider_configs")
    op.drop_table("ai_provider_configs")
