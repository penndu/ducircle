"""SQL 方言兼容测试。

重点是 `NULLS LAST` 这个坑。

MySQL 不支持 `ORDER BY col DESC NULLS LAST`，会报
`You have an error in your SQL syntax`。而 SQLite 支持，
所以本地测试全绿、线上直接 500。

这个测试锁定两种方言下生成的 SQL 都合法，
并且语义一致（NULL 都在最后）。
"""

from __future__ import annotations

import os

import pytest
import sqlalchemy as sa
from sqlalchemy.dialects import mysql, sqlite

from app.core import sql_compat


@pytest.fixture
def posts_table():
    return sa.Table(
        "posts",
        sa.MetaData(),
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("created", sa.DateTime()),
        sa.Column("ai_score", sa.Integer()),
    )


@pytest.fixture(autouse=True)
def restore_dialect():
    """每个测试后恢复方言环境变量，避免相互污染。"""
    original = os.environ.get("DUCIRCLE_DB_DIALECT")
    yield
    if original is None:
        os.environ.pop("DUCIRCLE_DB_DIALECT", None)
    else:
        os.environ["DUCIRCLE_DB_DIALECT"] = original


class TestDescNullsLast:
    def test_mysql_does_not_emit_nulls_last(self, posts_table):
        """MySQL 下绝不能出现 NULLS LAST。"""
        os.environ["DUCIRCLE_DB_DIALECT"] = "mysql"
        expr = sql_compat.desc_nulls_last(posts_table.c.created)
        sql = str(expr.compile(dialect=mysql.dialect()))
        assert "NULLS LAST" not in sql, f"MySQL 不支持这个语法：{sql}"
        assert "DESC" in sql

    def test_sqlite_keeps_nulls_last(self, posts_table):
        """SQLite 保留标准语法。"""
        os.environ["DUCIRCLE_DB_DIALECT"] = "sqlite"
        expr = sql_compat.desc_nulls_last(posts_table.c.created)
        sql = str(expr.compile(dialect=sqlite.dialect()))
        assert "NULLS LAST" in sql

    def test_asc_nulls_first_mysql(self, posts_table):
        """MySQL 的 ASC 本来就把 NULL 放最前，不加子句。"""
        os.environ["DUCIRCLE_DB_DIALECT"] = "mysql"
        expr = sql_compat.asc_nulls_first(posts_table.c.created)
        sql = str(expr.compile(dialect=mysql.dialect()))
        assert "NULLS FIRST" not in sql, f"MySQL 不支持这个语法：{sql}"

    def test_multi_columns(self, posts_table):
        """多列场景也要按方言处理。"""
        os.environ["DUCIRCLE_DB_DIALECT"] = "mysql"
        exprs = sql_compat.desc_nulls_last_multi(
            posts_table.c.ai_score, posts_table.c.created
        )
        for e in exprs:
            sql = str(e.compile(dialect=mysql.dialect()))
            assert "NULLS LAST" not in sql


class TestSemanticsMatch:
    """关键：两种方言的语义必须一致（NULL 都排最后）。"""

    def test_nulls_sorted_last_in_both_dialects(self, posts_table):
        os.environ["DUCIRCLE_DB_DIALECT"] = "mysql"
        mysql_sql = str(
            sql_compat.desc_nulls_last(posts_table.c.created).compile(
                dialect=mysql.dialect()
            )
        )
        os.environ["DUCIRCLE_DB_DIALECT"] = "sqlite"
        sqlite_sql = str(
            sql_compat.desc_nulls_last(posts_table.c.created).compile(
                dialect=sqlite.dialect()
            )
        )
        # 语义相同：都是「降序 + NULL 最后」
        assert "DESC" in mysql_sql and "DESC" in sqlite_sql
        assert "NULLS LAST" not in mysql_sql
        assert "NULLS LAST" in sqlite_sql


class TestDialectDetection:
    def test_defaults_to_mysql_without_env(self):
        """没设环境变量时按生产默认（MySQL）走——宁可生成 MySQL 合法 SQL，
        也不要在生产上生成 SQLite 才有的语法。"""
        os.environ.pop("DUCIRCLE_DB_DIALECT", None)
        # settings 的 mysql_host 默认是 'mysql'
        assert sql_compat.is_mysql() is True

    def test_env_var_takes_precedence(self):
        os.environ["DUCIRCLE_DB_DIALECT"] = "sqlite"
        assert sql_compat.is_mysql() is False
