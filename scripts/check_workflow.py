"""检查 GitHub Actions workflow 的结构问题。

背景：本地 PyYAML 能解析通过，但 GitHub 的解析器更严格，
会报一些本地发现不了的错误。

最典型的是 `with:` 下面放嵌套对象。GitHub 要求 with 的值都是
标量（字符串/数字/布尔），放对象会报：

  Invalid workflow file
  (Line: 188, Col: 13): A mapping was not expected

而 PyYAML 解析这种写法完全正常——它不知道 GitHub 的额外约束。

这个脚本按 GitHub 的实际约束检查，避免「本地过、CI 挂」。
"""

from __future__ import annotations

import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
WORKFLOW_DIR = ROOT / ".github" / "workflows"

# with 下面允许放对象的 action 参数（这些都是 action 自己定义的嵌套配置）
ALLOWED_MAPPING_KEYS = {
    "with": {"docker", "build-args", "cache-from", "cache-to", "build-contexts"},
}

# 步骤级合法字段
STEP_KEYS = {
    "name", "id", "if", "uses", "run", "with", "env",
    "continue-on-error", "timeout-minutes", "shell", "working-directory",
}

# job 级合法字段
JOB_KEYS = {
    "name", "needs", "runs-on", "permissions", "environment", "concurrency",
    "outputs", "env", "defaults", "if", "steps", "timeout-minutes",
    "strategy", "continue-on-error", "container", "services", "secrets",
}


def check_with_only_scalars(step: dict, where: str, problems: list[str]) -> None:
    """with 下的值必须都是标量，除非是 action 明确支持的嵌套参数。"""
    with_block = step.get("with")
    if not isinstance(with_block, dict):
        return

    for key, value in with_block.items():
        if isinstance(value, dict):
            problems.append(
                f"{where}：with 下的 '{key}' 是嵌套对象，"
                f"GitHub 会报 A mapping was not expected"
            )
        elif isinstance(value, list):
            problems.append(
                f"{where}：with 下的 '{key}' 是列表，"
                f"需要写成 JSON 字符串"
            )


def check_step_keys(step: dict, where: str, problems: list[str]) -> None:
    """步骤的键必须合法，env 放错层级是常见错误。"""
    for key in step:
        if key not in STEP_KEYS:
            problems.append(f"{where}：步骤里有未知字段 '{key}'")

    # env 必须是步骤的直接子键，不能藏在 with 里
    if isinstance(step.get("with"), dict) and "env" in step["with"]:
        problems.append(
            f"{where}：'env' 放在了 with 下面。"
            f"env 是步骤级字段，必须与 with 同级——"
            f"放在 with 里会被当成 action 参数"
        )


def check_job_keys(job: dict, name: str, problems: list[str]) -> None:
    for key in job:
        if key not in JOB_KEYS:
            problems.append(f"任务 '{name}'：未知字段 '{key}'")


def check_workflow(path: Path) -> list[str]:
    problems: list[str] = []

    try:
        data = yaml.safe_load(path.read_text())
    except yaml.YAMLError as exc:
        return [f"YAML 语法错误：{exc}"]

    if not isinstance(data, dict):
        return ["顶层不是映射"]

    # PyYAML 会把 on 解析成布尔 True
    if "on" not in data and True not in data:
        problems.append("缺少触发条件 on:")

    jobs = data.get("jobs") or {}
    if not jobs:
        problems.append("没有定义任何 job")
        return problems

    for job_name, job in jobs.items():
        if not isinstance(job, dict):
            problems.append(f"任务 '{job_name}' 不是映射")
            continue

        check_job_keys(job, job_name, problems)

        for idx, step in enumerate(job.get("steps") or [], 1):
            if not isinstance(step, dict):
                problems.append(f"任务 '{job_name}' 第 {idx} 步不是映射")
                continue
            where = f"任务 '{job_name}' 第 {idx} 步"
            check_step_keys(step, where, problems)
            check_with_only_scalars(step, where, problems)

            # 至少要有 uses 或 run
            if "uses" not in step and "run" not in step:
                problems.append(f"{where}：既没有 uses 也没有 run")

    return problems


def main() -> int:
    if not WORKFLOW_DIR.exists():
        print(f"没有 workflow 目录：{WORKFLOW_DIR}")
        return 0

    files = sorted(WORKFLOW_DIR.glob("*.yml")) + sorted(WORKFLOW_DIR.glob("*.yaml"))
    if not files:
        print("没有 workflow 文件")
        return 0

    all_problems: list[str] = []

    for path in files:
        problems = check_workflow(path)
        rel = path.relative_to(ROOT)
        if problems:
            print(f"[FAIL] {rel}")
            for p in problems:
                print(f"       - {p}")
            all_problems.extend(problems)
        else:
            print(f"[OK  ] {rel}")

    print()
    if all_problems:
        print("GitHub 也会拒绝这些配置。本地 PyYAML 能过不代表 CI 能过——")
        print("它不知道 action 参数的类型约束。")
        return 1

    print(f"{len(files)} 个 workflow 结构检查通过。")
    return 0


if __name__ == "__main__":
    sys.exit(main())