"""添加用户级采集频率配置

users 表加两个字段：
    crawl_cron     —— 文章采集频率（cron 表达式）
    link_page_cron —— 友链采集频率（cron 表达式）

手写而不是 autogenerate：autogenerate 会把已有表的约束重复创建
（那些在 0001 里已经建过），迁移会直接失败。

Revision ID: 0003_user_crawl_cron
Revises: 0002_link_pages
Create Date: 2026-10-24
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003_user_crawl_cron"
down_revision: str | None = "0002_link_pages"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column(
            "crawl_cron",
            sa.String(length=64),
            nullable=True,
            comment="文章采集频率，cron 表达式，空则用平台默认",
        ),
    )
    op.add_column(
        "users",
        sa.Column(
            "link_page_cron",
            sa.String(length=64),
            nullable=True,
            comment="友链采集频率，cron 表达式，空则用平台默认",
        ),
    )
    op.add_column(
        "users",
        sa.Column(
            "last_crawl_at",
            sa.DateTime(),
            nullable=True,
            comment="上次执行文章采集的时间",
        ),
    )
    op.add_column(
        "users",
        sa.Column(
            "last_link_page_scan_at",
            sa.DateTime(),
            nullable=True,
            comment="上次执行友链页检测的时间",
        ),
    )


def downgrade() -> None:
    op.drop_column("users", "last_link_page_scan_at")
    op.drop_column("users", "last_crawl_at")
    op.drop_column("users", "link_page_cron")
    op.drop_column("users", "crawl_cron")