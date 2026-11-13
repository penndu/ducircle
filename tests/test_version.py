"""版本号管理。

**单一真源**：版本号只写在 pyproject.toml 一处。
散落在 pyproject、main.py、Dockerfile 各写一遍，
迟早会「改了 A 忘了 B」——线上显示 1.0.0 实际跑的是 0.9 的代码。

两个版本概念别混：
- **语义版本**（1.0.0）：功能第几版 → pyproject.toml / APP_VERSION
- **构建版本**（202611121026）：哪次构建出来的 → CI 注入 DUCIRCLE_VERSION
"""

import re
import tomllib
from pathlib import Path


def _pyproject_version() -> str:
    data = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    return data["project"]["version"]


def _changelog_version() -> str:
    """从 CHANGELOG 的最新条目里读版本号。

    比硬编码断言好——每次升版不用改测试，
    免得「改了代码忘了改测试」变成「忘了改测试导致 CI 红」。
    """
    content = Path("CHANGELOG.md").read_text(encoding="utf-8")
    match = re.search(r"^## \[(\d+\.\d+\.\d+)\]", content, re.M)
    assert match, "CHANGELOG.md 里没有找到版本条目"
    return match.group(1)


class TestSingleSourceOfTruth:
    def test_pyproject_is_valid_semver(self):
        version = _pyproject_version()
        assert re.match(r"^\d+\.\d+\.\d+$", version), (
            f"版本号 {version} 不是语义版本格式"
        )

    def test_version_matches_pyproject(self):
        """pyproject 与 CHANGELOG 的版本号必须一致。

        不硬编码具体版本——升版时只要同步这两处就行，
        不用回来改测试。
        """
        assert _pyproject_version() == _changelog_version()

    def test_changelog_has_current_version_entry(self):
        assert f"## [{_pyproject_version()}]" in Path("CHANGELOG.md").read_text(
            encoding="utf-8"
        )

    def test_config_reads_from_pyproject(self):
        """settings.app_version 来自 pyproject，不是硬编码。"""
        from app.core.config import get_settings

        assert get_settings().app_version == _pyproject_version()

    def test_fastapi_uses_settings_version(self):
        """API 文档里的版本号要用 settings 的，不能另写一份。"""
        from app.core.config import get_settings
        from app.main import app

        assert app.version == get_settings().app_version

    def test_no_hardcoded_version_in_main(self):
        """main.py 里不该再有硬编码的版本号字面量。"""
        content = Path("app/main.py").read_text(encoding="utf-8")
        # 只允许 settings.app_version 这种引用
        assert not re.search(r'version\s*=\s*"\d+\.\d+\.\d+"', content), (
            "main.py 里出现了硬编码版本号"
        )


class TestBuildVersionIsSeparate:
    def test_dockerfile_injects_app_version(self):
        """Dockerfile 要注入 APP_VERSION（语义版本）。"""
        content = Path("deploy/Dockerfile").read_text(encoding="utf-8")
        assert "APP_VERSION" in content
        assert 'DUCIRCLE_VERSION="${BUILD_VERSION}"' in content, (
            "DUCIRCLE_VERSION 应该是构建时间戳，不是语义版本"
        )

    def test_ci_injects_app_version(self):
        """CI 要把 pyproject 里的版本号传给镜像。"""
        content = Path(".github/workflows/build.yml").read_text(encoding="utf-8")
        assert "APP_VERSION" in content
        assert "--build-arg APP_VERSION" in content

    def test_health_reports_both_versions(self):
        """/health 要同时给出语义版本和构建版本。"""
        from pathlib import Path as P

        content = P("app/main.py").read_text(encoding="utf-8")
        assert '"version": app.version' in content
        assert '"build": os.environ.get("DUCIRCLE_VERSION"' in content


class TestChangelog:
    def test_changelog_exists(self):
        path = Path("CHANGELOG.md")
        assert path.exists()
        content = path.read_text(encoding="utf-8")
        assert f"## [{_pyproject_version()}]" in content

    def test_readme_shows_version(self):
        content = Path("README.md").read_text(encoding="utf-8")
        assert f"v{_pyproject_version()}" in content
