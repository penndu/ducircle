"""兑换码：管理员批量生成，用户兑换成平台 AI 摘要次数

背景：平台默认不提供免费 AI 额度（``ai_daily_quota=0``），用户必须自带 Key。
但不是每个用户都愿意去申请服务商的 Key。于是加一条付费通道：
管理员批量生成兑换码，用户兑换后就用平台内置的 AI，次数从余额里扣。

两张新表：

- ``redeem_batches`` —— 一次「批量生成」的台账（面值、数量、生成人）
- ``redeem_codes``   —— 每张码的个体状态（未用 / 已用 / 已作废）

两个新列：

- ``users.ai_summary_credits`` —— 兑换来的**余额**（次数）。
  与 ``ai_daily_quota_override`` 不同：那个每天重置，这个可累加、不过期。
  回填 0，存量用户没有余额是事实。
- ``ai_quota_logs.from_credit`` —— 这笔消耗扣的是不是兑换余额。
  必须分开记，否则算「今日免费额度用了几篇」时会把用户花钱买的次数也算进去。

外键一律 ``ondelete=SET NULL`` 而不是 CASCADE：兑换码是钱换来的凭证，
用户注销或管理员离职都不该让兑换记录跟着蒸发。用户名另有冗余列可追溯。

Revision ID: 0012_redeem_codes
Revises: 0011_user_api_key_plain
Create Date: 2026-11-16
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0012_redeem_codes"
down_revision: str | None = "0011_user_api_key_plain"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ---- 1. 用户余额 ----
    op.add_column(
        "users",
        sa.Column(
            "ai_summary_credits",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )

    # ---- 2. 额度消耗分桶 ----
    op.add_column(
        "ai_quota_logs",
        sa.Column(
            "from_credit",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )

    # ---- 3. 批次台账 ----
    op.create_table(
        "redeem_batches",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("batch_no", sa.String(length=32), nullable=False),
        sa.Column("plan", sa.String(length=16), nullable=False),
        sa.Column("credits", sa.Integer(), nullable=False),
        sa.Column("price_cents", sa.Integer(), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("note", sa.String(length=255), nullable=True),
        sa.Column("created_by", sa.Integer(), nullable=True),
        sa.Column("created_by_name", sa.String(length=32), nullable=True),
        sa.Column(
            "exported", sa.Boolean(), nullable=False, server_default=sa.text("0")
        ),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["created_by"], ["users.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("batch_no"),
    )
    op.create_index("ix_redeem_batches_plan", "redeem_batches", ["plan"])

    # ---- 4. 兑换码 ----
    op.create_table(
        "redeem_codes",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("code", sa.String(length=32), nullable=False),
        sa.Column("batch_id", sa.Integer(), nullable=True),
        sa.Column("plan", sa.String(length=16), nullable=False),
        sa.Column("credits", sa.Integer(), nullable=False),
        sa.Column("price_cents", sa.Integer(), nullable=False),
        sa.Column("used_by", sa.Integer(), nullable=True),
        sa.Column("used_by_name", sa.String(length=32), nullable=True),
        sa.Column("used_at", sa.DateTime(), nullable=True),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
        sa.Column("revoked_by_name", sa.String(length=32), nullable=True),
        sa.Column("revoke_reason", sa.String(length=255), nullable=True),
        sa.Column("note", sa.String(length=255), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["batch_id"], ["redeem_batches.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(["used_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("code"),
    )
    op.create_index("ix_redeem_codes_batch", "redeem_codes", ["batch_id"])
    op.create_index("ix_redeem_codes_used_by", "redeem_codes", ["used_by"])
    op.create_index(
        "ix_redeem_codes_state", "redeem_codes", ["used_by", "revoked_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_redeem_codes_state", table_name="redeem_codes")
    op.drop_index("ix_redeem_codes_used_by", table_name="redeem_codes")
    op.drop_index("ix_redeem_codes_batch", table_name="redeem_codes")
    op.drop_table("redeem_codes")

    op.drop_index("ix_redeem_batches_plan", table_name="redeem_batches")
    op.drop_table("redeem_batches")

    op.drop_column("ai_quota_logs", "from_credit")
    op.drop_column("users", "ai_summary_credits")
