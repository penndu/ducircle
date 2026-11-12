"""给 sites 表加暂停采集的字段

背景：有些站点彻底挂了（域名过期、服务器下线），
现有的「连续失败 5 次降频」只是少来几次，还是每 7 天骚扰一次，
永远失败还污染失败列表。

新增 paused 字段：连续失败 10 次后自动置位，
定时抓取和批量抓取都会跳过，必须用户手动点「恢复采集」。

为什么不用 active 表达暂停：
active=False 是用户主动停用，语义不同——
不该在「恢复」时把用户自己关掉的站点意外打开。

Revision ID: 0006_site_paused
Revises: 0005_post_content_hash
Create Date: 2026-11-12
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006_site_paused"
down_revision: str | None = "0005_post_content_hash"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# 与 app.services.crawler.PAUSE_AFTER_FAILURES 保持一致
PAUSE_AFTER_FAILURES = 10


def upgrade() -> None:
    op.add_column(
        "sites",
        sa.Column("paused", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column("sites", sa.Column("paused_at", sa.DateTime(), nullable=True))
    op.add_column("sites", sa.Column("paused_reason", sa.Text(), nullable=True))

    # 回填：已经连续失败够多次的站点，直接置为暂停。
    # 否则迁移后它们还要再失败 10 次才停——白等。
    #
    # 原因文案用 Python 拼（不是 SQL 的 CONCAT）——
    # CONCAT 在 SQLite 里不存在，迁移要能在两种库上都跑通。
    conn = op.get_bind()
    rows = conn.execute(
        sa.text(
            """
            SELECT id, consecutive_failures, error_message
              FROM sites
             WHERE consecutive_failures >= :threshold
               AND deleted_at IS NULL
               AND paused = 0
            """
        ).bindparams(threshold=PAUSE_AFTER_FAILURES)
    ).fetchall()

    for site_id, failures, error_message in rows:
        reason = (
            f"连续失败 {failures} 次，已暂停采集。"
            f"最后一次错误：{error_message or '未知'}"
        )
        conn.execute(
            sa.text(
                "UPDATE sites SET paused = 1, paused_at = CURRENT_TIMESTAMP, "
                "paused_reason = :reason WHERE id = :id"
            ),
            {"reason": reason, "id": site_id},
        )


def downgrade() -> None:
    op.drop_column("sites", "paused_reason")
    op.drop_column("sites", "paused_at")
    op.drop_column("sites", "paused")
