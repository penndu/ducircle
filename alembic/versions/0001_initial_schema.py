"""初始数据模型

9 张表：users / sites / site_rules / posts / crawl_logs / tags /
site_tags / blacklist / ai_quota_logs

所有租户表都带 user_id，配合 app/core/tenant.py 的 ORM 事件钩子实现强制隔离。

Revision ID: 0001_initial
Revises:
Create Date: 2026-10-23
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001_initial"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        'users',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('username', sa.String(length=32), nullable=False),
        sa.Column('password_hash', sa.String(length=128), nullable=False),
        sa.Column('email', sa.String(length=128)),
        sa.Column('api_key_hash', sa.String(length=64)),
        sa.Column('is_active', sa.Boolean(), nullable=False),
        sa.Column('is_admin', sa.Boolean(), nullable=False),
        sa.Column('ai_provider', sa.String(length=32)),
        sa.Column('ai_api_key', sa.String(length=256)),
        sa.Column('ai_model', sa.String(length=128)),
        sa.Column('ai_enabled', sa.Boolean(), nullable=False),
        sa.Column('ai_daily_quota_override', sa.Integer()),
        sa.Column('max_concurrent_override', sa.Integer()),
        sa.Column('last_login_at', sa.DateTime()),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.Index('ix_users_created_at', 'created_at'),
        sa.Index('ix_users_username', 'username', unique=True),
    )

    op.create_table(
        'blacklist',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('users', ondelete='CASCADE'), nullable=False),
        sa.Column('pattern', sa.String(length=512), nullable=False),
        sa.Column('is_regex', sa.Boolean(), nullable=False),
        sa.Column('note', sa.String(length=256)),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.Index('ix_blacklist_created_at', 'created_at'),
        sa.Index('ix_blacklist_user_id', 'user_id'),
        sa.Index('ix_blacklist_user_pattern', 'user_id, pattern', unique=True),
    )

    op.create_table(
        'sites',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('users', ondelete='CASCADE'), nullable=False),
        sa.Column('name', sa.String(length=256), nullable=False),
        sa.Column('url', sa.String(length=1024), nullable=False),
        sa.Column('url_hash', sa.String(length=40), nullable=False),
        sa.Column('feed_url', sa.String(length=1024)),
        sa.Column('avatar', sa.String(length=1024)),
        sa.Column('description', sa.Text()),
        sa.Column('note', sa.Text()),
        sa.Column('source', sa.String(length=16), nullable=False),
        sa.Column('discovered_from', sa.String(length=1024)),
        sa.Column('strategy', sa.String(length=32)),
        sa.Column('framework', sa.String(length=32)),
        sa.Column('active', sa.Boolean(), nullable=False),
        sa.Column('error', sa.Boolean(), nullable=False),
        sa.Column('error_message', sa.Text()),
        sa.Column('consecutive_failures', sa.Integer(), nullable=False),
        sa.Column('last_crawled_at', sa.DateTime()),
        sa.Column('last_success_at', sa.DateTime()),
        sa.Column('crawl_interval_hours', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.Column('deleted_at', sa.DateTime()),
        sa.Index('ix_sites_created_at', 'created_at'),
        sa.Index('ix_sites_deleted_at', 'deleted_at'),
        sa.Index('ix_sites_last_success_at', 'last_success_at'),
        sa.Index('ix_sites_user_active', 'user_id, active'),
        sa.Index('ix_sites_user_hash', 'user_id, url_hash', unique=True),
        sa.Index('ix_sites_user_id', 'user_id'),
    )

    op.create_table(
        'tags',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('users', ondelete='CASCADE'), nullable=False),
        sa.Column('name', sa.String(length=64), nullable=False),
        sa.Column('color', sa.String(length=16)),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.Index('ix_tags_created_at', 'created_at'),
        sa.Index('ix_tags_user_id', 'user_id'),
    )

    op.create_table(
        'crawl_logs',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('users', ondelete='CASCADE'), nullable=False),
        sa.Column('site_id', sa.Integer(), sa.ForeignKey('sites', ondelete='CASCADE')),
        sa.Column('site_url', sa.String(length=1024), nullable=False),
        sa.Column('status', sa.String(length=16), nullable=False),
        sa.Column('strategy', sa.String(length=32)),
        sa.Column('error', sa.Text()),
        sa.Column('duration_ms', sa.Integer(), nullable=False),
        sa.Column('post_count', sa.Integer(), nullable=False),
        sa.Column('http_status', sa.Integer()),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.Index('ix_crawl_logs_created_at', 'created_at'),
        sa.Index('ix_crawl_logs_site_id', 'site_id'),
        sa.Index('ix_crawl_logs_user_id', 'user_id'),
        sa.Index('ix_logs_user_created', 'user_id, created_at'),
        sa.Index('ix_logs_user_status', 'user_id, status'),
    )

    op.create_table(
        'posts',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('users', ondelete='CASCADE'), nullable=False),
        sa.Column('site_id', sa.Integer(), sa.ForeignKey('sites', ondelete='CASCADE'), nullable=False),
        sa.Column('title', sa.String(length=512), nullable=False),
        sa.Column('link', sa.String(length=1024), nullable=False),
        sa.Column('guid_hash', sa.String(length=40), nullable=False),
        sa.Column('created', sa.DateTime()),
        sa.Column('updated', sa.DateTime()),
        sa.Column('crawled_at', sa.DateTime(), nullable=False),
        sa.Column('strategy', sa.String(length=32)),
        sa.Column('summary', sa.Text()),
        sa.Column('ai_model', sa.String(length=128)),
        sa.Column('ai_score', sa.Float()),
        sa.Column('ai_reason', sa.Text()),
        sa.Column('summary_content_hash', sa.String(length=40)),
        sa.Column('summary_at', sa.DateTime()),
        sa.Column('summary_edited', sa.Boolean(), nullable=False),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.Column('deleted_at', sa.DateTime()),
        sa.Index('ix_posts_created_at', 'created_at'),
        sa.Index('ix_posts_deleted_at', 'deleted_at'),
        sa.Index('ix_posts_site_id', 'site_id'),
        sa.Index('ix_posts_user_created', 'user_id, created'),
        sa.Index('ix_posts_user_id', 'user_id'),
        sa.Index('ix_posts_user_score', 'user_id, ai_score'),
    )

    op.create_table(
        'site_rules',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('users', ondelete='CASCADE'), nullable=False),
        sa.Column('site_id', sa.Integer(), sa.ForeignKey('sites', ondelete='CASCADE'), nullable=False),
        sa.Column('field', sa.String(length=16), nullable=False),
        sa.Column('selector', sa.String(length=512), nullable=False),
        sa.Column('attr', sa.String(length=32), nullable=False),
        sa.Column('priority', sa.Integer(), nullable=False),
        sa.Column('enabled', sa.Boolean(), nullable=False),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.Index('ix_rules_site_priority', 'site_id, priority'),
        sa.Index('ix_site_rules_created_at', 'created_at'),
        sa.Index('ix_site_rules_site_id', 'site_id'),
        sa.Index('ix_site_rules_user_id', 'user_id'),
    )

    op.create_table(
        'site_tags',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('users', ondelete='CASCADE'), nullable=False),
        sa.Column('site_id', sa.Integer(), sa.ForeignKey('sites', ondelete='CASCADE'), nullable=False),
        sa.Column('tag_id', sa.Integer(), sa.ForeignKey('tags', ondelete='CASCADE'), nullable=False),
        sa.Index('ix_site_tags_site_id', 'site_id'),
        sa.Index('ix_site_tags_tag_id', 'tag_id'),
        sa.Index('ix_site_tags_user_id', 'user_id'),
    )

    op.create_table(
        'ai_quota_logs',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('users', ondelete='CASCADE'), nullable=False),
        sa.Column('post_id', sa.Integer(), sa.ForeignKey('posts', ondelete='CASCADE'), nullable=False),
        sa.Column('quota_date', sa.Date(), nullable=False),
        sa.Column('provider', sa.String(length=32), nullable=False),
        sa.Column('model', sa.String(length=128)),
        sa.Column('from_user_key', sa.Boolean(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Index('ix_ai_quota_logs_post_id', 'post_id'),
        sa.Index('ix_ai_quota_logs_user_id', 'user_id'),
        sa.Index('ix_quota_user_date', 'user_id, quota_date'),
    )



def downgrade() -> None:
    op.drop_table("ai_quota_logs")
    op.drop_table("posts")
    op.drop_table("crawl_logs")
    op.drop_table("site_tags")
    op.drop_table("tags")
    op.drop_table("site_rules")
    op.drop_table("blacklist")
    op.drop_table("sites")
    op.drop_table("users")
