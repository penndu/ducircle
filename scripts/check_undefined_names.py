"""扫描代码里的「未定义名字」——专治后台任务的隐藏 bug。

背景：`scan_all_link_pages` 里用了 `User.link_page_cron`，
但文件顶部从没 import 过 `User`。这行代码平时没人执行，
等到调度器真的跑起来才炸：

    NameError: name 'User' is not defined

而调度器此前从未真正执行过，所以这个错一直没被发现。

这类错误靠人读代码很难扫干净——函数多、导入多、
拼写接近的名字更多。

**检查策略**（有意做得保守，避免误报变成噪音）：

一个名字如果在**整个文件里**的任何位置都没有被定义、
也没有被导入，就报出来。

这个判断比完整的词法作用域分析粗，但有两个好处：
1. 绝不会误报——只要文件里任何地方定义过，就算「有定义」
2. 能精准命中实际发生过的 bug——漏导入的模块级类/函数

漏报的情况是「同名的东西在别处定义了但这里没有」，
这种在实际代码里很少见，而且 ruff/pyflakes 能覆盖。
"""

from __future__ import annotations

import ast
import builtins
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP_DIR = ROOT / "app"

BUILTINS = set(dir(builtins)) | {
    "__file__", "__name__", "__doc__", "__module__", "__qualname__",
    "__annotations__", "__builtins__", "__spec__", "__loader__",
    "__package__", "__debug__", "self", "cls",
}


class FileScope:
    """收集一个文件里「定义过」和「使用过」的所有名字。"""

    def __init__(self) -> None:
        self.defined: set[str] = set()
        self.used: list[tuple[str, int]] = []

    def add_defined(self, name: str) -> None:
        self.defined.add(name)

    def add_used(self, name: str, lineno: int) -> None:
        self.used.append((name, lineno))


def collect(tree: ast.AST, scope: FileScope) -> None:
    """遍历整棵树，收集所有定义与使用。"""
    for node in ast.walk(tree):
        # ---- 定义 ----
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                scope.add_defined(alias.asname or alias.name.split(".")[0])

        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            scope.add_defined(node.name)
            # 装饰器、返回注解、参数注解里的名字也算「使用」
            for deco in node.decorator_list:
                _collect_expr_names(deco, scope)
            for arg in (
                list(node.args.args)
                + list(node.args.kwonlyargs)
                + list(node.args.posonlyargs)
            ):
                scope.add_defined(arg.arg)
                if arg.annotation is not None:
                    _collect_expr_names(arg.annotation, scope)
            if node.args.vararg:
                scope.add_defined(node.args.vararg.arg)
            if node.args.kwarg:
                scope.add_defined(node.args.kwarg.arg)
            if node.returns is not None:
                _collect_expr_names(node.returns, scope)
            # 默认参数里的名字也是使用
            for default in node.args.defaults + [
                d for d in node.args.kw_defaults if d is not None
            ]:
                _collect_expr_names(default, scope)

        elif isinstance(node, ast.ClassDef):
            scope.add_defined(node.name)
            for deco in node.decorator_list:
                _collect_expr_names(deco, scope)
            for base in node.bases:
                _collect_expr_names(base, scope)
            for kw in node.keywords:
                _collect_expr_names(kw.value, scope)

        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            scope.add_defined(node.id)

        elif isinstance(node, ast.ExceptHandler) and node.name:
            scope.add_defined(node.name)

        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            scope.defined.update(node.names)

        elif isinstance(node, ast.Assign):
            for t in node.targets:
                _collect_expr_names(t, scope)

        elif isinstance(node, ast.AnnAssign):
            _collect_expr_names(node.target, scope)
            if node.annotation is not None:
                _collect_expr_names(node.annotation, scope)

        # ---- 使用 ----
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            scope.add_used(node.id, node.lineno)

        elif isinstance(node, ast.Lambda):
            # lambda 的参数是局部变量，不能当未定义名
            for arg in (
                list(node.args.args)
                + list(node.args.kwonlyargs)
                + list(node.args.posonlyargs)
            ):
                scope.add_defined(arg.arg)
            if node.args.vararg:
                scope.add_defined(node.args.vararg.arg)
            if node.args.kwarg:
                scope.add_defined(node.args.kwarg.arg)
            _collect_expr_names(node.body, scope)
            for default in node.args.defaults + [
                d for d in node.args.kw_defaults if d is not None
            ]:
                _collect_expr_names(default, scope)

        elif isinstance(node, (ast.ListComp, ast.SetComp, ast.GeneratorExp)):
            # 推导式的循环变量只在推导式内有效，
            # 但保守起见也算「文件里定义过」——避免误报。
            for gen in node.generators:
                for name in ast.walk(gen.target):
                    if isinstance(name, ast.Name):
                        scope.add_defined(name.id)
                _collect_expr_names(gen.iter, scope)
                for cond in gen.ifs:
                    _collect_expr_names(cond, scope)
            _collect_expr_names(node.elt, scope)

        elif isinstance(node, ast.DictComp):
            for gen in node.generators:
                for name in ast.walk(gen.target):
                    if isinstance(name, ast.Name):
                        scope.add_defined(name.id)
                _collect_expr_names(gen.iter, scope)
                for cond in gen.ifs:
                    _collect_expr_names(cond, scope)
            _collect_expr_names(node.key, scope)
            _collect_expr_names(node.value, scope)

        elif isinstance(node, ast.Attribute):
            # ``User.link_page_cron`` ——基名 User 也是使用
            if isinstance(node.value, ast.Name):
                scope.add_used(node.value.id, node.value.lineno)
            _collect_expr_names(node.value, scope)

        elif isinstance(node, ast.Assign):
            for t in node.targets:
                _collect_expr_names(t, scope)


def _collect_expr_names(node: ast.AST, scope: FileScope) -> None:
    """收集表达式里用到的所有名字（含属性访问的基名）。"""
    for sub in ast.walk(node):
        if isinstance(sub, ast.Name) and isinstance(sub.ctx, ast.Load):
            scope.add_used(sub.id, sub.lineno)
        elif isinstance(sub, ast.Attribute):
            if isinstance(sub.value, ast.Name):
                scope.add_used(sub.value.id, sub.value.lineno)


def check_file(path: Path) -> list[str]:
    tree = ast.parse(path.read_text("utf-8"))
    scope = FileScope()
    collect(tree, scope)

    problems: list[str] = []
    for name, lineno in scope.used:
        if name in scope.defined or name in BUILTINS:
            continue
        problems.append(
            f"{path.relative_to(ROOT)}:{lineno} 未定义的名字 '{name}'"
        )
    return problems


def main() -> int:
    problems: list[str] = []

    for path in sorted(APP_DIR.rglob("*.py")):
        try:
            problems.extend(check_file(path))
        except SyntaxError as exc:
            problems.append(f"{path.relative_to(ROOT)} 语法错误：{exc}")

    if problems:
        print("[FAIL] 存在未定义的名字：")
        for p in sorted(set(problems)):
            print(f"       - {p}")
        print()
        print("这类错误只有代码真正执行到那行才会暴露。")
        print("后台任务此前从未执行过，所以一直没被发现。")
        return 1

    print("未定义名字检查通过。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
