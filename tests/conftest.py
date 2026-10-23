"""pytest 全局配置与通用夹具。"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture(autouse=True)
def _reset_tenant_context():
    """每个用例前后清空租户上下文，避免跨用例泄漏。

    ContextVar 在同一线程内不会自动重置，不清理的话
    一个用例设置的租户会影响后续所有用例。
    """
    from app.core.tenant import _tenant_bypass, _tenant_id

    _tenant_id.set(None)
    _tenant_bypass.set(False)
    yield
    _tenant_id.set(None)
    _tenant_bypass.set(False)
