"""给 posts 表加 summary_failed 标记

背景：摘要生成遇到「不可重试」的失败（模型内容安全拦截）时，
每次批量生成都会重新选中它——白费时间和额度。
实测「每天60秒读懂全世界」这类新闻聚合文章必然被拦，
每轮都要重试一次。

标记后批量生成直接跳过，用户手动点「重新生成」才重试。

Revision ID: 0008_post_summary_failed
Revises: 0007_post_raw_content
Create Date: 2026-11-12
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008_post_summary_failed"
down_revision: str | None = "0007_post_raw_content"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "posts",
        sa.Column(
            "summary_failed", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
    )
    op.add_column("posts", sa.Column("summary_error", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("posts", "summary_error")
    op.drop_column("posts", "summary_failed")
