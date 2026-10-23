"""检查 .dockerignore 是否排除了构建必需的文件。

这是我踩过的坑：为了精简构建上下文加了 .dockerignore，
顺手把 *.md 排掉了 —— 但 pyproject.toml 声明了 readme = "README.md"，
hatchling 构建时校验文件存在，直接 OSError 终止。

CI 连续三轮都挂在这一步，每轮只能看到「构建并推送」failed，
看不到原因。本脚本让这类问题在提交前就被发现。
"""

from __future__ import annotations

import fnmatch
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load_patterns() -> list[str]:
    path = ROOT / ".dockerignore"
    if not path.exists():
        return []
    patterns = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            patterns.append(line)
    return patterns


def is_ignored(target: str, patterns: list[str]) -> bool:
    """判断某个路径是否被 dockerignore 命中。

    规则按 docker 的语义简化：目录模式排除其下所有内容，
    路径模式精确匹配。
    """
    path = Path(target)
    for pat in patterns:
        is_dir_pattern = pat.endswith("/")
        clean = pat.rstrip("/")

        # 目标本身匹配
        if fnmatch.fnmatch(target, pat) or fnmatch.fnmatch(path.name, clean):
            return True

        # 位于被排除的目录内
        for parent in path.parents:
            parent_str = str(parent)
            if parent_str == ".":
                break
            if fnmatch.fnmatch(parent_str, pat) or fnmatch.fnmatch(
                parent_str + "/", pat
            ):
                return True
            if fnmatch.fnmatch(Path(parent_str).name, clean):
                return True
    return False


def required_files() -> list[str]:
    """从 pyproject.toml 和 Dockerfile 里推出构建必需的文件。"""
    required: list[str] = []

    pyproject = (ROOT / "pyproject.toml").read_text()
    # readme 字段
    match = re.search(r'^readme\s*=\s*"([^"]+)"', pyproject, re.M)
    if match:
        required.append(match.group(1))

    # Dockerfile 里 COPY 的本地路径
    dockerfile = ROOT / "deploy" / "Dockerfile"
    if dockerfile.exists():
        for line in dockerfile.read_text().splitlines():
            line = line.strip()
            if not line.startswith("COPY"):
                continue
            # 跨阶段 COPY（--from=builder）的源来自上一个阶段的镜像，
            # 不在构建上下文里，与 .dockerignore 无关。
            if "--from=" in line:
                continue
            # 过滤所有 --xxx 标志（--chown、--from 等）
            parts = [p for p in line.split() if not p.startswith("--")]
            if len(parts) < 3:
                continue
            for src in parts[1:-1]:  # 去掉 COPY 关键字和目标
                src = src.rstrip("/")
                if src.startswith("./"):
                    src = src[2:]
                # 跳过变量与通配
                if "$" in src or "*" in src:
                    continue
                required.append(src)

    return required


def main() -> int:
    patterns = load_patterns()
    if not patterns:
        print(".dockerignore 不存在——构建上下文会包含整个目录")
        print("本机 .venv 通常上百MB，在 CI 上传会超时。建议加上 .dockerignore。")
        return 1

    required = required_files()
    problems = []

    print(f"检查 {len(required)} 个构建必需路径是否被 .dockerignore 排除：")

    for target in required:
        exists = (ROOT / target).exists()
        ignored = is_ignored(target, patterns)
        status = "❌ 被排除" if ignored else "✓ 保留"
        if not exists:
            status = "❌ 文件不存在"
            problems.append(f"{target}（文件不存在）")
        elif ignored:
            problems.append(target)
        print(f"  [{status}] {target}")

    print()
    if problems:
        print("构建会失败。必须修正 .dockerignore：")
        for p in problems:
            print(f"  - {p}")
        print()
        print("尤其是 readme 文件：pyproject.toml 里的readme 字段指向它，")
        print("hatchling 构建时会校验存在性，缺了直接报错。")
        return 1

    print("构建必需文件都没被排除。")
    return 0


if __name__ == "__main__":
    sys.exit(main())