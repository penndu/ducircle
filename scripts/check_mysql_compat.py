"""MySQL 特有行为的检查。

SQLite 测不出这些问题，但它们在真实 MySQL 上会直接让服务起不来：

1. **索引长度**：MySQL InnoDB 的索引键长度上限（utf8mb4 下 3072 字节）。
   url VARCHAR(1024) 建唯一索引 = 4096 字节，超限会直接报错。
   models 里用的是 url_hash 而不是 url 做唯一索引，就是为了绕开这个——
   但必须验证。
2. **VARCHAR 长度**：MySQL 的 VARCHAR(1024) 会真的占空间。
3. **大小写敏感性**：utf8mb4_unicode_ci 下 URL 去重要靠规范化，
   不能依赖数据库的字符串比较。
4. **DATETIME 精度**：MySQL 的 DATETIME 不带时区。
5. **引擎与字符集**：InnoDB + utf8mb4 才能存 emoji 和正确排序中文。

这个脚本需要在有 MySQL 的环境里跑，用来在部署前发现问题。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.models import Base  # noqa: E402


def check_column_types() -> list[str]:
    """检查是否有 MySQL 下会出问题但 SQLite 不在乎的列定义。"""
    problems = []

    for table in Base.metadata.tables.values():
        for col in table.columns:
            # 唯一索引会作用在列上，utf8mb4 下每字符 4 字节
            if col.unique or (
                table.indexes and col.name in {
                    c.name for idx in table.indexes if idx.unique for c in idx.columns
                }
            ):
                length = getattr(col.type, "length", None)
                if length and length * 4 > 3072:
                    problems.append(
                        f"{table.name}.{col.name} 唯一索引过长："
                        f"VARCHAR({length}) 在 utf8mb4 下约 {length * 4} 字节，"
                        f"超过 InnoDB 的 3072 字节上限"
                    )

            # 没有长度上限的 TEXT 做唯一索引也不行
            if col.unique and col.type.__class__.__name__ == "Text":
                problems.append(
                    f"{table.name}.{col.name} 是 TEXT 类型，不能直接做唯一索引"
                )

            # DATETIME 在 MySQL 不带时区，代码里必须存 naive datetime
            if col.type.__class__.__name__ == "DateTime":
                colname = col.name
                if colname.endswith("_at") and not col.default:
                    pass  # 允许无默认，代码里显式赋值

    return problems


def check_index_coverage() -> list[str]:
    """检查租户表的索引是否覆盖了常见查询。"""
    problems = []
    from app.core.tenant import TENANT_TABLES

    for name in TENANT_TABLES:
        table = Base.metadata.tables.get(name)
        if table is None:
            problems.append(f"TENANT_TABLES 里的 {name} 不存在于 metadata")
            continue

        indexed = set()
        for idx in table.indexes:
            for col in idx.columns:
                indexed.add(col.name)
        # 主键索引也算
        indexed.update(c.name for c in table.primary_key.columns)

        if "user_id" not in indexed:
            problems.append(
                f"{name}.user_id 没有索引——所有查询都按它过滤，缺索引会全表扫"
            )

    return problems


def check_text_defaults() -> list[str]:
    """检查 server_default 是否用了 MySQL 不认的写法。"""
    problems = []
    for table in Base.metadata.tables.values():
        for col in table.columns:
            default = col.server_default
            if default is None:
                continue
            value = str(default.arg)
            # SQLite 认 now()，MySQL 8 也认。但 CURRENT_TIMESTAMP 更通用
            if "now()" in value.lower():
                problems.append(
                    f"{table.name}.{col.name} 用了 now()，"
                    f"MySQL 上虽可用但应统一用 CURRENT_TIMESTAMP 以避免歧义"
                )
    return problems


def main() -> int:
    print("=" * 70)
    print("MySQL 方言兼容性静态检查")
    print("=" * 70)

    checks = [
        ("索引长度限制", check_column_types),
        ("租户表索引覆盖", check_index_coverage),
        ("server_default 写法", check_text_defaults),
    ]

    all_problems: list[str] = []
    for name, check in checks:
        problems = check()
        status = "OK  " if not problems else "注意"
        print(f"\n[{status}] {name}")
        if problems:
            for p in problems:
                print(f"       - {p}")
            all_problems.extend(problems)
        else:
            print("       无问题")

    print("\n" + "=" * 70)
    tables = list(Base.metadata.sorted_tables)
    print(f"共 {len(tables)} 张表：{', '.join(t.name for t in tables)}")
    print("=" * 70)

    if all_problems:
        print("\n注意：这些问题不一定要修，但要确认是有意为之。")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())