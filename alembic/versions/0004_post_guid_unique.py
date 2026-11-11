"""给 posts 表补唯一约束 (user_id, guid_hash)

背景：模型里声明了 UniqueConstraint，但 0001 建表时没建出来，
生产库里因此出现了重复的 (user_id, guid_hash) 行。
落库逻辑用 scalar_one_or_none() 查重复文章，撞到多行就抛
MultipleResultsFound —— 表现为「某个站点怎么抓都失败」。

迁移做两件事：
1. 清理重复行（保留 id 最小的那条，其余软删除）
2. 补上唯一约束

Revision ID: 0004_post_guid_unique
Revises: 0003_user_crawl_cron
Create Date: 2026-11-11
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004_post_guid_unique"
down_revision: str | None = "0003_user_crawl_cron"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 1) 清理重复行。
    #    保留每组里 id 最小的（最早抓到的那条），其余软删除——
    #    软删除而不是物理删除，是为了让已经生成的摘要不丢。
    op.execute(
        sa.text(
            """
            UPDATE posts
            SET deleted_at = NOW()
            WHERE deleted_at IS NULL
              AND id NOT IN (
                SELECT keep_id FROM (
                    SELECT MIN(id) AS keep_id
                    FROM posts
                    WHERE deleted_at IS NULL
                    GROUP BY user_id, guid_hash
                ) t
              )
            """
        )
    )

    # 2) 补唯一约束。
    #    先确保没有残留重复，否则 MySQL 会拒绝创建。
    op.execute(
        sa.text(
            """
            UPDATE posts p1
            JOIN posts p2
              ON p1.user_id = p2.user_id
             AND p1.guid_hash = p2.guid_hash
             AND p1.id > p2.id
            SET p1.deleted_at = NOW()
            WHERE p1.deleted_at IS NULL AND p2.deleted_at IS NULL
            """
        )
    )

    op.create_unique_constraint(
        "uq_post_user_guid", "posts", ["user_id", "guid_hash"]
    )


def downgrade() -> None:
    op.drop_constraint("uq_post_user_guid", "posts", type_="unique")
