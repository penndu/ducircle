"""给 posts 表加 content_hash，修复跨永久链接的重复文章

背景：obaby@mars（zhongxiaojie.com）站点上发现同一篇文章被采了三次——
  id=206  https://zhongxiaojie.com/2026/10/2281/   （旧日期式永久链接）
  id=368  https://zhongxiaojie.com/post/2281/       （改成 post 永久链接后）
  id=480  https://zhongxiaojie.cn/post/2281/        （feed 里域名变成 .cn）

三者的 URL 不同 → guid_hash 不同 → 同一篇文章被当成三篇入库。
guId_hash 按完整 URL 算，这是它的固有局限：URL 变了就认不出是同一篇。

修法：新增 content_hash 列，基于「站点 + 文章 ID」算指纹。
WordPress 的永久链接不管怎么改，末段的 post ID 都不变（2281），
配合站点 id 就能稳定识别同一篇文章。

为什么指纹要带 site_id：
两个不同的 WordPress 站完全可能都有 post id 2281。
只按 ID 算会把它们误判成同一篇，然后错误地合并/删除。

回填为什么用 Python 而不是 SQL 正则：
MySQL 的 REGEXP 和 SQLite 的实现不同，迁移要能在两种库上都跑通。
数据量是几百到几千行，Python 遍历完全够用。

Revision ID: 0005_post_content_hash
Revises: 0004_post_guid_unique
Create Date: 2026-11-12
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005_post_content_hash"
down_revision: str | None = "0004_post_guid_unique"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _content_hash(site_id: int, article_id: str) -> str:
    """站点 + 文章 ID 的指纹。

    带 site_id 是为了区分不同站点的相同文章 ID——
    两个 WordPress 站都可能有 /post/2281/，那不是同一篇。

    ⚠️ 格式必须和 app.core.security.content_hash() 一致，
    否则回填的指纹和运行时新算的对不上，等于没去重。
    """
    raw = f"site:{site_id}:id:wp:{article_id}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def _article_id(link: str) -> str | None:
    """从链接里提取文章 ID。

    只认路径末段是纯数字、且长度 >= 2 的情况——
    「/1/」「/2/」这类太短的数字更可能是分页或列表页，
    误判会把不相干的文章合并掉。
    """
    try:
        path = link.split("://", 1)[-1].split("/", 1)[-1]
    except IndexError:
        return None
    last = path.rstrip("/").split("/")[-1]
    if not last.isdigit() or len(last) < 2:
        return None
    return last


def upgrade() -> None:
    # 1) 加列
    op.add_column(
        "posts",
        sa.Column("content_hash", sa.String(length=40), nullable=True),
    )
    op.create_index(
        "ix_posts_content_hash", "posts", ["content_hash"], unique=False
    )

    # 2) 回填 + 清理重复
    conn = op.get_bind()
    rows = conn.execute(
        sa.text(
            """
            SELECT id, site_id, link, guid_hash, deleted_at
            FROM posts
            WHERE deleted_at IS NULL
            ORDER BY id
            """
        )
    ).fetchall()

    # 按 (site_id, 文章ID) 分组
    groups: dict[tuple[int, str], list[tuple]] = {}
    for row in rows:
        article_id = _article_id(row.link)
        if not article_id:
            continue
        groups.setdefault((row.site_id, article_id), []).append(row)

    for (site_id, article_id), items in groups.items():
        chash = _content_hash(site_id, article_id)
        # 组里只有一篇：只是回填指纹，不动数据
        if len(items) == 1:
            conn.execute(
                sa.text("UPDATE posts SET content_hash = :ch WHERE id = :id"),
                {"ch": chash, "id": items[0][0]},
            )
            continue

        # 有多篇：按「link 最像正文地址」挑保留哪条。
        # 优先保留 path 段数少的（/post/2281/ 比 /2026/10/2281/ 更像正文），
        # 其次保留 id 最小（最早抓到，摘要等数据都在它身上）。
        def _rank(item):
            path = item[2].split("://", 1)[-1].split("/", 1)[-1].rstrip("/")
            return (path.count("/"), item[0])

        items.sort(key=_rank)
        keeper = items[0]

        # 保留的那条补上 content_hash
        conn.execute(
            sa.text("UPDATE posts SET content_hash = :ch WHERE id = :id"),
            {"ch": chash, "id": keeper[0]},
        )
        # 其余的软删除——保留数据痕迹，但不再出现在文章流里。
        # ⚠️ 用软删除而不是物理删：文章可能已被 AI 摘要、被前端引用过，
        # 物理删会让那些关联数据悬空。软删除的行 deleted_at 非 NULL，
        # 也不会和唯一约束撞车。
        for item in items[1:]:
            conn.execute(
                sa.text(
                    "UPDATE posts SET deleted_at = NOW(), content_hash = :ch "
                    "WHERE id = :id"
                ),
                {"ch": chash, "id": item[0]},
            )


def downgrade() -> None:
    op.drop_index("ix_posts_content_hash", table_name="posts")
    op.drop_column("posts", "content_hash")
