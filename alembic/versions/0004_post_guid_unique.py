"""给 posts 表补唯一约束

背景：模型里声明了 UniqueConstraint，但 0001 建表时没建出来，
生产库里因此出现了重复的 (user_id, guid_hash) 行。
落库逻辑用 scalar_one_or_none() 查重复文章，撞到多行就抛
MultipleResultsFound —— 表现为「某个站点怎么抓都失败」。

⚠️ 约束为什么是三列而不是两列：
MySQL 的唯一索引里，NULL 值不参与唯一性判断。
posts 是软删除表（deleted_at 可空），如果约束只含
(user_id, guid_hash)，那么「已软删除的那条」和「新插入的一条」
会撞车——而这恰恰是正常业务：文章删掉后重新抓回来是允许的。
加上 deleted_at 之后，软删除的行 deleted_at 非 NULL，
不会和新行冲突；两行都未删除时 deleted_at 同为 NULL，
MySQL 允许多行 NULL——所以这里还要在应用层保证只留一条。
真正的保障是「未删除的行」上的唯一性，见下面的清理逻辑。

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
    #    保留每组里 id 最小的那条（最早抓到的那条，摘要等数据都在它身上），
    #    其余物理删除——软删除在这里没用，因为 MySQL 唯一索引里
    #    NULL 不参与去重，删掉的行照样会和新行撞车。
    op.execute(
        sa.text(
            """
            DELETE p FROM posts p
            JOIN posts keep
              ON p.user_id = keep.user_id
             AND p.guid_hash = keep.guid_hash
             AND p.id > keep.id
            """
        )
    )

    # 2) 补唯一约束。三列版，兼容软删除。
    op.create_unique_constraint(
        "uq_post_user_guid", "posts", ["user_id", "guid_hash", "deleted_at"]
    )


def downgrade() -> None:
    op.drop_constraint("uq_post_user_guid", "posts", type_="unique")
