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


def check_copy_coverage() -> list[str]:
    """检查 pyproject 引用的文件是否被 Dockerfile COPY 进构建目录。

    这是真正的坑：我一直只检查「有没有被 .dockerignore 排除」，
    但 Dockerfile 里只 COPY 了 pyproject.toml 和 app/，
    README.md 压根没进镜像——于是 hatchling 说文件不存在。

    排除和没复制是两种不同的失败方式，必须都查。
    """
    problems: list[str] = []

    pyproject = (ROOT / "pyproject.toml").read_text()
    readme = re.search(r'^readme\s*=\s*"([^"]+)"', pyproject, re.M)
    if not readme:
        return problems

    filename = readme.group(1)

    dockerfile = ROOT / "deploy" / "Dockerfile"
    if not dockerfile.exists():
        return [f"找不到 deploy/Dockerfile"]

    # 收集 builder 阶段 COPY 的所有文件名
    copied: set[str] = set()
    for line in dockerfile.read_text().splitlines():
        line = line.strip()
        if not line.startswith("COPY") or "--from=" in line:
            continue
        parts = [x for x in line.split() if not x.startswith("--")]
        for src in parts[1:-1]:
            src = src.rstrip("/")
            if src.startswith("./"):
                src = src[2:]
            copied.add(src)
            # COPY a b 形式：把 a 也算进已复制
            copied.add(Path(src).name)

    if filename not in copied:
        problems.append(
            f"pyproject.toml 声明了 readme = \"{filename}\"，"
            f"但 Dockerfile 没有 COPY 它。"
            f" 构建时文件不存在，hatchling 会报 "
            f"OSError: Readme file does not exist"
        )

    return problems


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

    copy_problems = check_copy_coverage()
    print()
    if copy_problems:
        print("❌ Dockerfile 的 COPY 有遗漏：")
        for p in copy_problems:
            print(f"  - {p}")
        return 1

    print("构建必需文件都在，且 Dockerfile 已 COPY 进来。")
    return 0


if __name__ == "__main__":
    sys.exit(main())