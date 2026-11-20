"""用户可选 AI 模式：平台 AI（扣次数）/ 自定义 AI（自带 Key）

背景：改造前是「有 Key 就走自己的 Key、没 Key 才走平台」的隐式规则。
用户没法在两者之间主动切换——填了 Key 就用不了兑换次数，
想改用平台 AI 只能先删 Key，而删了又得重新粘贴，很别扭。

新增 ``users.ai_mode``：

- ``platform`` —— 用平台内置 provider/key/model，扣每日额度或兑换余额
- ``custom`` —— 用用户自带的 provider/key/model，不扣平台任何额度

回填规则（保证升级前后行为不变）：

- 已有 ai_api_key 的用户 → custom（和改造前一样走自己的 Key）
- 没有 Key 的用户       → platform（和改造前一样走平台）

Revision ID: 0013_user_ai_mode
Revises: 0012_redeem_codes
Create Date: 2026-11-20
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0013_user_ai_mode"
down_revision: str | None = "0012_redeem_codes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column(
            "ai_mode",
            sa.String(length=16),
            nullable=False,
            server_default=sa.text("'platform'"),
        ),
    )
    # 有 Key 的老用户保持原行为（走自己的 Key，不扣次数）。
    # 空串也当作没配——库里理论上不会有，但表达式写严谨点不吃亏。
    op.execute(
        "UPDATE users SET ai_mode = 'custom' "
        "WHERE ai_api_key IS NOT NULL AND ai_api_key <> ''"
    )


def downgrade() -> None:
    op.drop_column("users", "ai_mode")
