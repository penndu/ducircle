"""路由完整性检查。

这类 bug 极其隐蔽：一个缺失的 import 会让**整个函数的依赖注入静默失效**——
路由还在，但所有 Depends 都不执行，参数变成未 await 的协程，
报错信息还指向别处。必须靠自动检查守住。
"""

from __future__ import annotations

from typing import get_type_hints

import pytest

from app.main import app


def _all_route_endpoints() -> list[tuple[str, object]]:
    """从 FastAPI app 收集所有路由处理函数。"""
    seen: dict[str, object] = {}
    for route in app.routes:
        # FastAPI 0.161+ 用 _IncludedRouter 包装，需递归展开
        router = getattr(route, "original_router", None)
        if router is not None:
            for sub in router.routes:
                for path, methods in _iter_routes(sub):
                    for method in methods:
                        seen[f"{method} {path}"] = sub.endpoint
            continue
        for path, methods in _iter_routes(route):
            for method in methods:
                seen[f"{method} {path}"] = route.endpoint
    return sorted(seen.items())


def _iter_routes(route) -> list[tuple[str, list[str]]]:
    path = getattr(route, "path", None)
    methods = getattr(route, "methods", None)
    if not path or not methods:
        return []
    return [(path, list(methods))]


ENDPOINTS = _all_route_endpoints()


class TestRouteAnnotations:
    def test_endpoints_discovered(self):
        """先确认扫描到了路由，否则下面的检查是空转。"""
        assert len(ENDPOINTS) >= 30, f"只扫到 {len(ENDPOINTS)} 个路由，扫描逻辑可能失效"

    @pytest.mark.parametrize(
        "label,endpoint", ENDPOINTS, ids=[x[0] for x in ENDPOINTS]
    )
    def test_type_hints_resolvable(self, label, endpoint):
        """所有注解必须能解析。

        缺一个 import 就会让 get_type_hints 抛 NameError，
        连带整个函数的依赖注入失效——这是最难查的一类 bug。
        """
        try:
            get_type_hints(endpoint)
        except NameError as exc:
            pytest.fail(
                f"{label} 的注解无法解析：{exc}。"
                f"通常是漏了 import。修复后在 handler 里加 Depends(...) 显式依赖。"
            )
        except Exception as exc:  # noqa: BLE001
            pytest.fail(f"{label} 注解解析失败：{type(exc).__name__}: {exc}")

    @pytest.mark.parametrize(
        "label,endpoint", ENDPOINTS, ids=[x[0] for x in ENDPOINTS]
    )
    def test_session_params_have_depends(self, label, endpoint):
        """AsyncSession 类型的参数必须带 Depends(get_db)。

        漏掉 Depends 时，FastAPI 会把协程对象本身传进去，
        表现为 `'coroutine' object has no attribute 'execute'`。

        判断依据是 get_type_hints(include_extras=True)——
        正确写法的形态是 Annotated[AsyncSession, Depends(...)]。
        """
        import inspect

        from fastapi import params
        from sqlalchemy.ext.asyncio import AsyncSession

        try:
            hints = get_type_hints(endpoint, include_extras=True)
        except Exception:  # noqa: BLE001
            pytest.skip("注解解析已由 test_type_hints_resolvable 覆盖")

        sig = inspect.signature(endpoint)
        for name in sig.parameters:
            hint = hints.get(name)
            if hint is None:
                continue

            base = getattr(hint, "__origin__", None)
            args = getattr(hint, "__args__", ())

            # 取出 Annotated 里被包装的真实类型
            real_type = args[0] if str(base) == "typing.Annotated" and args else hint
            if real_type is not AsyncSession:
                continue

            metadata = args[1:] if str(base) == "typing.Annotated" else ()
            has_depends = any(
                isinstance(m, params.Depends) for m in metadata
            ) or isinstance(sig.parameters[name].default, params.Depends)

            assert has_depends, (
                f"{label} 的参数 {name} 是 AsyncSession 但没有 Depends(get_db)，"
                f"实际传进去的会是未 await 的协程"
            )


class TestAppWiring:
    def test_all_expected_routers_mounted(self):
        paths = set(app.openapi()["paths"].keys())

        expected = [
            # 认证
            "/api/auth/register",
            "/api/auth/login",
            "/api/auth/logout",
            # 友链
            "/api/sites",
            "/api/sites/{site_id}",
            "/api/sites/{site_id}/rules",
            # 只读 API
            "/v1/all",
            "/v1/friends",
            # 后台管理
            "/api/admin/crawl-logs",
            "/api/admin/failed-sites",
            "/api/admin/stats",
            # 用户设置
            "/api/users/me",
            # 运维
            "/health",
        ]
        missing = [p for p in expected if p not in paths]
        assert not missing, f"这些端点没挂上：{missing}"

    def test_web_pages_not_in_openapi(self):
        """页面路由不该出现在 OpenAPI 里——它们是给浏览器看的，不是 API。"""
        paths = app.openapi()["paths"].keys()
        web_paths = [p for p in paths if p.startswith("/web")]
        assert not web_paths, f"页面路由混进了 OpenAPI：{web_paths}"

class TestAwaitUsage:
    """防止 `await db.execute(q).method()` 这类隐蔽错误。

    Python 解析为 `await (db.execute(q).method())`，方法实际作用在协程上，
    报 'coroutine' object has no attribute 'xxx'。
    这个错误只在运行时暴露，且报错信息指向别处，极难定位。
    """

    def test_no_misplaced_await(self):
        from pathlib import Path

        from scripts.check_await_usage import find_bad_awaits

        problems: list[str] = []
        for path in sorted(Path("app").rglob("*.py")):
            for lineno, segment in find_bad_awaits(path.read_text()):
                problems.append(f"{path}:{lineno}  {segment}")

        assert not problems, "发现 await 用法错误：\n" + "\n".join(problems)
