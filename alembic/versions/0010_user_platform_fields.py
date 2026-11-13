"""users 表补充平台管理字段

v2.0.0 起平台支持多用户：访客可自助注册，管理员有独立的管理页面，
可以禁用账号、调整额度、重置密码。

管理页面需要两个新字段：

- ``admin_note``：管理员给账号写的备注。封禁、限额、工单核对时，
  光看用户名想不起来「这个号为什么被停」，备注是唯一的现场记录。
- ``last_login_ip``：最后登录来源 IP。批量注册、撞库攻击的典型特征
  是「同一 IP 注册/登录几十个账号」，有这个字段管理员才能一眼看出来。

两列都允许 NULL，不需要回填——存量用户没有历史 IP 是事实，
不该编一个假值。SQLite 与 MySQL 都能直接加列，无兼容性风险。

Revision ID: 0010_user_platform_fields
Revises: 0009_user_password_changed_at
Create Date: 2026-11-13
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010_user_platform_fields"
down_revision: str | None = "0009_user_password_changed_at"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "users", sa.Column("admin_note", sa.String(length=255), nullable=True)
    )
    op.add_column(
        "users", sa.Column("last_login_ip", sa.String(length=64), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("users", "last_login_ip")
    op.drop_column("users", "admin_note")
