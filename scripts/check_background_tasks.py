"""检查后台任务的两个「静默失败」陷阱。

背景：这两个问题都不会让程序崩溃，只会让任务「看起来在跑」，
实际上一件事都没做——线上排查时非常费时间。

**陷阱一：add_job 传 lambda**

APScheduler 用 `iscoroutinefunction_partial()` 判断 job.func 是不是协程
函数。不是的话，它把调用丢进线程池的 `run_in_executor`，
而线程池无法 await 协程——协程对象被创建出来就丢掉了。

APScheduler 那边只看到「调用没抛异常」，照样打印
"Job ... executed successfully"。日志一片绿，功能全没跑。

**陷阱二：同步上下文管理器用在 `async with` 里**

`no_tenant_filter()` 是 @contextmanager（同步），
配 `async with` 会抛：
    TypeError: '_GeneratorContextManager' object does not support
    the asynchronous context manager protocol

而这些异常通常被后台框架的 except 吞掉，表现同样是「没反应」。

这两个检查用 AST 做静态分析，不依赖运行——
因为它们恰恰是「运行了才知道」的那类问题。
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP_DIR = ROOT / "app"

# 只支持同步协议的上下文管理器，不能用在 async with 里
SYNC_ONLY_MANAGERS = {
    "no_tenant_filter",
    "as_tenant",
}


def _check_scheduler_jobs(problems: list[str]) -> None:
    """add_job 的第一个参数必须是协程函数，不能是 lambda。"""
    path = APP_DIR / "services" / "scheduler.py"
    if not path.exists():
        return

    tree = ast.parse(path.read_text("utf-8"))

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute) and func.attr == "add_job"):
            continue
        if not node.args:
            problems.append(
                f"services/scheduler.py:{node.lineno} add_job 没有指定函数"
            )
            continue

        first = node.args[0]
        if isinstance(first, ast.Lambda):
            problems.append(
                f"services/scheduler.py:{first.lineno} "
                f"add_job 用了 lambda —— 它不是协程函数，"
                f"APScheduler 会丢进线程池，协程从未被 await，任务不会执行。"
                f"请改用 _job() 包装"
            )


def _check_async_context_managers(problems: list[str]) -> None:
    """async with 里不得使用只支持同步协议的上下文管理器。"""
    for path in sorted(APP_DIR.rglob("*.py")):
        tree = ast.parse(path.read_text("utf-8"))
        rel = path.relative_to(ROOT)

        for node in ast.walk(tree):
            if not isinstance(node, ast.AsyncWith):
                continue

            for item in node.items:
                expr = item.context_expr
                name = None

                if isinstance(expr, ast.Call) and isinstance(expr.func, ast.Name):
                    name = expr.func.id
                elif isinstance(expr, ast.Name):
                    name = expr.id

                if name in SYNC_ONLY_MANAGERS:
                    problems.append(
                        f"{rel}:{node.lineno} async with {name} —— "
                        f"它是同步上下文管理器，运行期会抛 TypeError。"
                        f"异步代码请用 {name}_async()"
                    )


def main() -> int:
    problems: list[str] = []

    _check_scheduler_jobs(problems)
    _check_async_context_managers(problems)

    if problems:
        print("[FAIL] 后台任务检查未通过：")
        for p in problems:
            print(f"       - {p}")
        print()
        print("这些问题不会让程序崩溃，只会让任务「看起来在跑、实际啥也没干」。")
        return 1

    print("后台任务检查通过（调度任务协程化 + 异步上下文管理器用法）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
