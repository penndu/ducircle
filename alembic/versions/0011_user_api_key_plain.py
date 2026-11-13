"""users 表保存 API Key 明文，支持设置页查看

v2.0.1 起，设置页可以直接看到当前 API Key。

在此之前只存哈希，明文只在注册成功时返回一次。用户没当场复制，
设置页就永远显示「（暂无）」——看到这个提示会以为 Key 丢了，
于是去点「重新生成」，而重新生成必然改变 Key，博客前端随即全线 401。
**「看不到」被逼成了「Key 变了」**，这正是不该出现的结果。

存量用户这一列为 NULL（明文确实无从还原），
需要通过设置页的「导入已有 Key」认领一次——认领只写明文副本，
`api_key_hash` 不动，所以 Key 本身保持不变。

Revision ID: 0011_user_api_key_plain
Revises: 0010_user_platform_fields
Create Date: 2026-11-13
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011_user_api_key_plain"
down_revision: str | None = "0010_user_platform_fields"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "users", sa.Column("api_key_plain", sa.String(length=64), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("users", "api_key_plain")
