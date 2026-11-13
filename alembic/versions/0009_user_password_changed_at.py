"""给 users 表加 password_changed_at

背景：Session 是无状态签名（itsdangerous），本来就没法主动踢人下线。
加了改密码功能后，如果不做处理，用户改完密码，
旧浏览器上的 Cookie 还能继续用满 7 天——密码改了等于没改。

有了这个时间戳，校验会话时比一下签发时间，
早于改密时刻签发的会话一律拒绝。

存量用户回填成「注册时间」——
他们的会话都是在注册之后签发的，不受影响；
而从此刻起改密码，旧会话立刻失效。

Revision ID: 0009_user_password_changed_at
Revises: 0008_post_summary_failed
Create Date: 2026-11-13
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009_user_password_changed_at"
down_revision: str | None = "0008_post_summary_failed"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "users", sa.Column("password_changed_at", sa.BigInteger(), nullable=True)
    )

    # 回填成「注册时的 Unix 时间戳」——存量用户的会话都在这之后签发，
    # 不受影响；而从此刻起改密码，旧会话立刻失效。
    #
    # 不能留 NULL：校验逻辑遇到 NULL 会跳过时间比较，
    # 将来用户改了密码，旧会话仍然能用。
    conn = op.get_bind()
    conn.execute(
        sa.text(
            """
            UPDATE users
               SET password_changed_at = UNIX_TIMESTAMP(created_at)
             WHERE password_changed_at IS NULL
               AND created_at IS NOT NULL
            """
        )
    )


def downgrade() -> None:
    op.drop_column("users", "password_changed_at")
