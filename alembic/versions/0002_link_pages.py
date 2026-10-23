"""添加友链页表

关注的友链页：填一个友链页地址，系统解析出上面的所有友情链接
并加入友链列表，之后定期重新检测，发现新友链自动补进来。

手写而不是 autogenerate：autogenerate 会把已有表的唯一约束
重复创建一遍（那些在 0001 里已经建过），迁移会直接失败。

Revision ID: 0002_link_pages
Revises: 0001_initial
Create Date: 2026-10-24
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_link_pages"
down_revision: str | None = "0001_initial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "link_pages",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("url", sa.String(length=1024), nullable=False),
        sa.Column(
            "url_hash",
            sa.String(length=40),
            nullable=False,
            comment="规范化后 URL 的 SHA1，用于同页去重",
        ),
        sa.Column(
            "name",
            sa.String(length=256),
            nullable=True,
            comment="页面标题，解析时自动填",
        ),
        sa.Column("note", sa.String(length=256), nullable=True),
        sa.Column("found_count", sa.Integer(), nullable=False),
        sa.Column("added_count", sa.Integer(), nullable=False),
        sa.Column("last_checked_at", sa.DateTime(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("active", sa.Boolean(), nullable=False),
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
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_link_pages_created_at"), "link_pages", ["created_at"], unique=False)
    op.create_index(
        "ix_link_pages_user_hash", "link_pages", ["user_id", "url_hash"], unique=True
    )
    # user_id 的单列索引由模型的 index=True 自动创建（名字 ix_link_pages_user_id），
    # 不要在这里再建一个同名的——MySQL 上同名索引会冲突，
    # downgrade 时也会因为「找不到该索引」报错。
    # 0001 迁移也是同样处理：user_id 列不带 index=True，
    # 需要时用 sa.Index 显式声明复合索引。


def downgrade() -> None:
    # 顺序有讲究：MySQL 的外键约束会「借用」user_id 上的索引
    # （ix_link_pages_user_hash 的首列就是 user_id）。
    # 不先删外键就删索引，会报
    #   Can't drop index ... needed in a foreign key constraint
    #
    # 外键名不能写死——MySQL 自动生成的名字（link_pages_ibfk_1）
    # 在不同环境下可能不同。先查实际名字再删。
    fk_name = _find_foreign_key_name()
    if fk_name:
        op.drop_constraint(fk_name, "link_pages", type_="foreignkey")
    op.drop_index("ix_link_pages_user_hash", table_name="link_pages")
    op.drop_index(op.f("ix_link_pages_created_at"), table_name="link_pages")
    op.drop_table("link_pages")


def _find_foreign_key_name() -> str | None:
    """查出 link_pages 上指向 users.id 的外键约束名。"""
    from sqlalchemy import inspect

    inspector = inspect(op.get_bind())
    for fk in inspector.get_foreign_keys("link_pages"):
        if fk.get("referred_table") == "users":
            return fk.get("name")
    return None