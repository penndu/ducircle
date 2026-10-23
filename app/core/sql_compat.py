"""SQL 方言兼容工具。

解决一个具体的坑：**MySQL 不支持 `NULLS LAST`**。

SQLAlchemy 的 `Column.desc().nullslast()` 会无条件生成
`ORDER BY col DESC NULLS LAST`：
- SQLite 支持 ✓
- PostgreSQL 支持 ✓
- **MySQL 不支持 ✗** → `You have an error in your SQL syntax`

而 MySQL 本身的行为恰好就是我们要的：DESC 时 NULL 天然排在最后，
ASC 时排在最前。所以根本不需要 `NULLS LAST` 子句。

问题在于本地测试全用 SQLite，这段 SQL 在测试里跑得好好的，
到线上 MySQL 才炸。线上首次访问文章列表页就是 500。

解决方式：按实际使用的方言选择实现。

用法：`order_by(sql_compat.desc_nulls_last(Post.created))`

方言判断的依据是「应用实际连的库」：
- 环境变量 DUCIRCLE_DB_DIALECT 由启动时确定（见 app/db.py）
- 测试用 SQLite，线上用 MySQL
"""

from __future__ import annotations

import os
from typing import Any

from sqlalchemy import ColumnElement, desc


def current_dialect_name() -> str:
    """返回当前使用的方言名（sqlite / mysql）。

    优先读环境变量——那是引擎创建时确定的，最准确。
    读不到就退回到配置里的主机名判断。
    """
    name = os.environ.get("DUCIRCLE_DB_DIALECT")
    if name:
        return name.lower()

    try:
        from app.core.config import get_settings

        host = get_settings().mysql_host
        # 容器里的服务名是 1Panel-mysql-wQr4 或 mysql；
        # SQLite 环境下这个字段没意义，但测试会设置上面的环境变量。
        if host and "mysql" in host.lower():
            return "mysql"
    except Exception:  # noqa: BLE001
        pass

    return "mysql"  # 生产默认


def is_mysql() -> bool:
    return current_dialect_name() == "mysql"


def desc_nulls_last(column: ColumnElement[Any]) -> ColumnElement[Any]:
    """降序排序，NULL 排在最后。

    - MySQL：生成 `col DESC`（MySQL 的 DESC 本来就把 NULL 放最后）
    - 其它方言：生成 `col DESC NULLS LAST`（标准 SQL）
    """
    if is_mysql():
        return desc(column)
    return desc(column).nullslast()


def asc_nulls_first(column: ColumnElement[Any]) -> ColumnElement[Any]:
    """升序排序，NULL 排在最前。

    - MySQL：生成 `col ASC`（MySQL 的 ASC 本来就把 NULL 放最前）
    - 其它方言：生成 `col ASC NULLS FIRST`
    """
    if is_mysql():
        return column.asc()
    return column.asc().nullsfirst()


def desc_nulls_last_multi(*columns: ColumnElement[Any]) -> list[ColumnElement[Any]]:
    """多个列统一降序、NULL 最后。"""
    return [desc_nulls_last(c) for c in columns]
