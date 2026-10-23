"""从 SQLAlchemy metadata 生成初始迁移脚本。

`alembic revision --autogenerate` 必须连上数据库才能比对 schema。
首次建表其实不需要比对——直接按 metadata 编译 CREATE TABLE 即可。
本脚本用于生成初始迁移，之后的变更再走正常的 autogenerate 流程。
"""

import ast
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.dialects import mysql  # noqa: E402
from sqlalchemy.schema import CreateIndex, CreateTable  # noqa: E402

from app.models import Base  # noqa: E402


def main() -> None:
    dialect = mysql.dialect()
    out: list[str] = []

    for table in Base.metadata.sorted_tables:
        out.append("    op.create_table(")
        out.append(f"        {table.name!r},")
        for col in table.columns:
            col_type = _render_type(col, dialect)

            # 位置参数：列名、类型
            parts = [repr(col.name), col_type]

            # 位置参数必须在 keyword 之前，ForeignKey 同理。
            for fk in col.foreign_keys:
                target = f"{fk.column.table.name}.{fk.column.name}"
                ondelete = fk.ondelete or "NO ACTION"
                parts.append(f"sa.ForeignKey({target!r}, ondelete={ondelete!r})")

            # keyword 参数
            if col.primary_key:
                parts.append("primary_key=True")
                if col.autoincrement is not False:
                    parts.append("autoincrement=True")
            if col.server_default is not None:
                default = col.server_default.arg
                if isinstance(default, str):
                    parts.append(f"server_default={default!r}")
                else:
                    parts.append(f"server_default=sa.text({str(default)!r})")
            if not col.nullable and not col.primary_key:
                parts.append("nullable=False")

            out.append(f"        sa.Column({', '.join(parts)}),")

        for idx in sorted(table.indexes, key=lambda i: i.name or ""):
            # 注意：sa.Index 的列参数必须用 **varargs**，不能是列表也不能是
            # 逗号连接的字符串——两者都会报 "String column name or column
            # expression for DDL constraint expected"。
            # 列表形式 ['a','b'] 只在 op.create_index() 里才成立。
            cols = [c.name for c in idx.columns]
            unique = ", unique=True" if idx.unique else ""
            col_args = ", ".join(repr(c) for c in cols)
            out.append(f"        sa.Index({idx.name!r}, {col_args}{unique}),")
        out.append("    )")
        out.append("")

    print("\n".join(out))
    print(f"# 共 {len(Base.metadata.sorted_tables)} 张表")


def self_check(body: str) -> list[str]:
    """生成后自检——在真实数据库上跑之前就能抓到的问题。

    这几个坑全部是在真实 MySQL 上验证迁移时才暴露的，
    SQLite 上一个都发现不了（因为测试根本不走迁移脚本）：
      1. sa.Index('name', 'a, b') —— 逗号字符串被当成单个列名
      2. sa.Index('name', ['a','b']) —— 列表只支持 op.create_index
      3. sa.ForeignKey('users') —— 缺列名，必须写 'users.id'
    正确写法：sa.Index('name', 'a', 'b') / sa.ForeignKey('users.id')
    """
    problems = []
    # 生成的是缩进后的函数体片段，不能直接 parse，要先包一层
    try:
        tree = ast.parse("def _f():\n" + body)
    except SyntaxError as exc:
        return [f"生成的代码语法错误（第 {exc.lineno} 行）：{exc.msg}"]

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute):
            continue

        args = node.args

        if func.attr == "Index":
            if len(args) < 2:
                continue
            col_arg = args[1]
            # 错法一：列表 —— 只有 op.create_index() 才接受
            if isinstance(col_arg, (ast.List, ast.Tuple)):
                problems.append(
                    f"第 {node.lineno} 行：sa.Index 的列参数用了列表，"
                    f"sa.Index 只接受 varargs"
                )
            # 错法二：含逗号的字符串 —— 会被当成一个叫 "a, b" 的列
            elif isinstance(col_arg, ast.Constant) and isinstance(col_arg.value, str):
                if "," in col_arg.value:
                    problems.append(
                        f"第 {node.lineno} 行：sa.Index 的列参数是含逗号的字符串 "
                        f"{col_arg.value!r}，应该用 varargs"
                    )

        elif func.attr == "ForeignKey":
            # 必须写成 '表.列'，只写表名会报
            # "table 'users' has no column named 'user_id'"
            if args:
                target = args[0]
                if isinstance(target, ast.Constant) and isinstance(target.value, str):
                    if "." not in target.value:
                        problems.append(
                            f"第 {node.lineno} 行：ForeignKey({target.value!r}) "
                            f"缺列名，应写成 '表.列' 形式"
                        )

    return problems


def _render_type(col, dialect) -> str:
    """把列类型渲染成 sa.XXX(...) 形式。

    不能直接用 repr(col.type)——那得到的是 "String(length=32)"，
    缺 sa. 前缀，在迁移脚本里是 NameError。
    注意 Integer/Text/DateTime 等是通用类型，在 sqlalchemy 顶层；
    只有 Numeric/String 的长度约束等才是 mysql 方言的实现。
    """
    import sqlalchemy as sa

    t = col.type
    # 模型里全部使用 sqlalchemy 顶层通用类型（sa.Integer/sa.String 等），
    # 因此按顶层类型判断即可；mysql 方言的 INTEGER/VARCHAR 等是编译产物。
    if isinstance(t, sa.Integer) and not isinstance(t, sa.BigInteger):
        return "sa.Integer()"
    if isinstance(t, sa.BigInteger):
        return "sa.BigInteger()"
    if isinstance(t, sa.Boolean):
        return "sa.Boolean()"
    if isinstance(t, sa.Text):
        return "sa.Text()"
    if isinstance(t, sa.DateTime):
        return "sa.DateTime()"
    if isinstance(t, sa.Date):
        return "sa.Date()"
    if isinstance(t, sa.Float):
        return "sa.Float()"
    if isinstance(t, sa.Numeric):
        return f"sa.Numeric(precision={t.precision}, scale={t.scale})"
    if isinstance(t, sa.String):
        return f"sa.String(length={t.length})"
    # 兜底：用方言编译出的 DDL 类型名反查
    fallback = t.compile(dialect=dialect)
    raise TypeError(
        f"未处理的列类型 {type(t).__name__}（{fallback}），请在 _render_type 中补充"
    )


if __name__ == "__main__":
    import io
    import contextlib

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        main()
    body = buf.getvalue()

    problems = self_check(body)
    if problems:
        print("自检发现问题：", file=sys.stderr)
        for p in problems:
            print(f"  {p}", file=sys.stderr)
        sys.exit(1)

    print(body)