"""给 posts 表加 raw_content 字段

背景：摘要生成 90% 的文章失败在「正文抽取失败」上。
实测 obaby@mars（zhongxiaojie.com）的文章页只返回 1700 字节的
SPA 骨架页，正文全靠 JS 渲染——静态抓取拿不到。

但这些站点的 RSS feed 里带着全文（WordPress 默认输出 content:encoded）。
抓取时顺手存下来，摘要生成直接用，不用再抓页面。

这列是正文缓存，不是业务数据，截断到 20000 字符足够摘要用。

Revision ID: 0007_post_raw_content
Revises: 0006_site_paused
Create Date: 2026-11-12
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007_post_raw_content"
down_revision: str | None = "0006_site_paused"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "posts", sa.Column("raw_content", sa.Text(), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("posts", "raw_content")
