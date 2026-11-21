"""用户可选摘要触发方式：自动 / 手动

新增 ``users.summary_mode``：

- ``auto``（默认）—— 定时任务照旧为新文章生成摘要
- ``manual`` —— 定时任务跳过，只在文章页手动批量生成

存量用户全部回填 ``auto``，升级前后行为一致：不会因为版本升级
突然停掉所有人的自动摘要。

Revision ID: 0014_user_summary_mode
Revises: 0013_user_ai_mode
Create Date: 2026-11-21
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0014_user_summary_mode"
down_revision: str | None = "0013_user_ai_mode"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column(
            "summary_mode",
            sa.String(length=16),
            nullable=False,
            server_default=sa.text("'auto'"),
        ),
    )
    op.execute("UPDATE users SET summary_mode = 'auto' WHERE summary_mode IS NULL OR summary_mode = ''")


def downgrade() -> None:
    op.drop_column("users", "summary_mode")
