"""从 SQLAlchemy metadata 生成初始迁移脚本。

`alembic revision --autogenerate` 必须连上数据库才能比对 schema。
首次建表其实不需要比对——直接按 metadata 编译 CREATE TABLE 即可。
本脚本用于生成初始迁移，之后的变更再走正常的 autogenerate 流程。
"""

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
                target = fk.column.table.name
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
            cols = ", ".join(c.name for c in idx.columns)
            unique = ", unique=True" if idx.unique else ""
            out.append(f"        sa.Index({idx.name!r}, {cols!r}{unique}),")
        out.append("    )")
        out.append("")

    print("\n".join(out))
    print(f"# 共 {len(Base.metadata.sorted_tables)} 张表")


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
    main()