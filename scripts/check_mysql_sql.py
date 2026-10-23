"""检查生成的 SQL 在 MySQL 上是否合法。

用 MySQL 方言编译所有涉及排序的语句，扫出 MySQL 不支持的语法。

MySQL 不支持的写法（SQLite/PostgreSQL 支持）：
- NULLS LAST
- NULLS FIRST
- FILTER (WHERE ...)
- RETURNING
- 某些窗口函数语法

这个脚本的价值：SQLite 测试全绿不代表 MySQL 能跑。
线上第一次访问文章列表就 500 的教训。
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# MySQL 不支持的 SQL 语法片段
FORBIDDEN = [
    (r"\bNULLS\s+LAST\b", "NULLS LAST", "MySQL 不支持，用纯 DESC（MySQL 的 DESC 本就把 NULL 放最后）"),
    (r"\bNULLS\s+FIRST\b", "NULLS FIRST", "MySQL 不支持，用纯 ASC"),
    (r"\bFILTER\s*\(", "FILTER (WHERE ...)", "MySQL 不支持 FILTER 子句"),
    (r"\bRETURNING\b", "RETURNING", "MySQL 不支持 RETURNING"),
    (r"\bILIKE\b", "ILIKE", "MySQL 用 LIKE 代替（默认不区分大小写）"),
    (r"\bSIMILAR\s+TO\b", "SIMILAR TO", "MySQL 用 REGEXP 代替"),
    (r"\bAT\s+TIME\s+ZONE\b", "AT TIME ZONE", "MySQL 用 CONVERT_TZ 代替"),
]


def _strip_comments_and_docs(text: str) -> str:
    """去掉注释和 docstring，只留真正的代码。

    否则「文档里提到 NULLS LAST」也会被当成问题——
    而恰恰是 app/core/sql_compat.py 的文档需要提到它。
    """
    import io
    import tokenize

    out = []
    try:
        tokens = tokenize.generate_tokens(io.StringIO(text).readline)
        for tok in tokens:
            if tok.type in (tokenize.COMMENT, tokenize.STRING):
                continue
            out.append(tok.string)
    except (tokenize.TokenError, IndentationError):
        return text
    return " ".join(out)


def check_source() -> list[str]:
    """静态扫描源码里直接写这些语法的代码。

    注释和字符串常量要排除——否则文档里提到 NULLS LAST
    也会被当成问题。
    """
    problems = []
    for path in ROOT.glob("app/**/*.py"):
        # sql_compat.py 是那个「合法的使用点」——
        # 它按方言决定用不用 NULLS LAST，自己当然要用。
        if path.name == "sql_compat.py":
            continue

        raw = path.read_text()
        code = _strip_comments_and_docs(raw)

        # 单独检查：有没有人绕过 sql_compat 直接调 .nullslast() / .nullsfirst()
        # 这才是最初的 bug 形态——只检查 sql_compat 生成的 SQL 是盲区。
        #
        # 注意：tokenize 会把 .nullslast() 拆成 nullslast 和 ( ) 并插空格，
        # 所以匹配时不能写 ".nullslast()"，要按标识符 + 括号分开匹配。
        for method, name in (("nullslast", "NULLS LAST"), ("nullsfirst", "NULLS FIRST")):
            if re.search(rf"\b{method}\s*\(", code):
                problems.append(
                    f"{path.relative_to(ROOT)} 直接调用了 .{method}() —— "
                    f"这会生成 MySQL 不支持的 {name}。"
                    f"请改用 app.core.sql_compat 的 desc_nulls_last() / asc_nulls_first()"
                )

        for pattern, name, hint in FORBIDDEN:
            for m in re.finditer(pattern, code, re.IGNORECASE):
                # 在剥离后的文本里定位不到行号，
                # 换成在原文里找同位置的片段做提示
                snippet = code[max(0, m.start() - 40) : m.end() + 20].strip()
                problems.append(
                    f"{path.relative_to(ROOT)} 代码里出现 {name} —— {hint}"
                    f"\n         片段: {snippet[:80]}"
                )
    return problems


def check_compiled() -> list[str]:
    """用 MySQL 方言编译真实的排序语句。"""
    os.environ["DUCIRCLE_DB_DIALECT"] = "mysql"

    problems: list[str] = []

    try:
        import sqlalchemy as sa
        from sqlalchemy.dialects import mysql

        from app.core import sql_compat
        from app.models import Post, Site, Tag, CrawlLog

        tables = {"posts": Post, "sites": Site, "tags": Tag, "crawl_logs": CrawlLog}

        for name, model in tables.items():
            for col_name in ("created", "updated", "id", "name"):
                col = getattr(model, col_name, None)
                if col is None:
                    continue
                try:
                    stmt = sa.select(model).order_by(
                        sql_compat.desc_nulls_last(col), model.id.desc()
                    )
                    sql = str(stmt.compile(dialect=mysql.dialect()))
                except Exception as exc:  # noqa: BLE001
                    problems.append(f"{name}.{col_name} 编译失败：{exc}")
                    continue

                for pattern, bad, hint in FORBIDDEN:
                    if re.search(pattern, sql, re.IGNORECASE):
                        problems.append(
                            f"{name}.{col_name} 生成的 SQL 含 {bad} —— {hint}"
                        )

    except Exception as exc:  # noqa: BLE001
        problems.append(f"编译检查失败：{exc}")

    return problems


def main() -> int:
    print("=== 源码静态扫描 ===")
    src_problems = check_source()
    for p in src_problems:
        print(f"  [FAIL] {p}")
    if not src_problems:
        print("  [OK  ] 没有直接写 MySQL 不支持的语法")

    print()
    print("=== MySQL 方言编译检查 ===")
    comp_problems = check_compiled()
    for p in comp_problems:
        print(f"  [FAIL] {p}")
    if not comp_problems:
        print("  [OK  ] 生成的 SQL 在 MySQL 上合法")

    print()
    if src_problems or comp_problems:
        print("发现 MySQL 兼容性问题，会导致线上 500。")
        return 1

    print("MySQL 兼容性检查通过。")
    return 0


if __name__ == "__main__":
    sys.exit(main())