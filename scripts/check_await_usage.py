"""检测 `await X.execute(...).method()` 写法。

这是异步 SQLAlchemy 里最隐蔽的错误：
    x = await db.execute(q).scalar_one()
Python 解析为 `await (db.execute(q).scalar_one())`，
.scalar_one() 实际作用在协程对象上，报
「'coroutine' object has no attribute 'scalar_one'」。

正确写法必须是：
    x = (await db.execute(q)).scalar_one()

用 AST 检测而非正则——正则匹配不过跨行的调用参数。
"""

from __future__ import annotations

import ast
import pathlib

# execute/scalar/scalars 之后直接跟方法调用的都是错误
ASYNC_METHODS = {"execute", "scalar", "scalars", "stream", "scalar_one"}


def find_bad_awaits(source: str) -> list[tuple[int, str]]:
    """返回 [(行号, 错误代码片段)]。"""
    tree = ast.parse(source)
    bad: list[tuple[int, str]] = []

    for node in ast.walk(tree):
        # 模式： Await( Call(Attribute(Call(Attribute(...,'execute'))), '.scalar_one') )
        if not isinstance(node, ast.Await):
            continue
        value = node.value
        if not isinstance(value, ast.Call):
            continue
        func = value.func
        if not isinstance(func, ast.Attribute):
            continue
        # func.value 必须是另一个 Call（即 execute(...) 的返回值）
        inner = func.value
        if not isinstance(inner, ast.Call):
            continue
        inner_func = inner.func
        if isinstance(inner_func, ast.Attribute) and inner_func.attr in ASYNC_METHODS:
            segment = ast.unparse(node)
            bad.append((node.lineno, segment))

    return bad


def main() -> int:
    root = pathlib.Path("app")
    total = 0

    for path in sorted(root.rglob("*.py")):
        source = path.read_text()
        bad = find_bad_awaits(source)
        if not bad:
            continue
        print(f"\n{path}")
        for lineno, segment in bad:
            print(f"  第 {lineno} 行: {segment[:100]}")
            total += 1

    if total:
        print(f"\n共发现 {total} 处。")
        print("修法：把 .method() 挪到 await 的结果上")
        print("  错:  x = await db.execute(q).scalar_one()")
        print("  对:  x = (await db.execute(q)).scalar_one()")
        return 1

    print("未发现问题。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())