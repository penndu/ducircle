"""后台任务的回归测试。

这些用例针对两个曾经让线上「看起来一切正常、实际什么都没跑」的 bug：

1. APScheduler 的 job.func 写成 lambda —— 它不是协程函数，
   APScheduler 会把它丢进线程池，协程对象从未被 await，
   任务主体一次都没执行过，但日志照样打印 "executed successfully"。

2. 同步的 no_tenant_filter() 配 `async with` —— 运行期抛
   TypeError: '_GeneratorContextManager' object does not support
   the asynchronous context manager protocol。

两个 bug 的共同点：异常被后台框架的 except 吞掉，
用户点了「立即抓取」只看到「已触发」，库里一行数据都没有。
"""

from __future__ import annotations

import ast
import inspect
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


class TestSchedulerJobIsCoroutine:
    """调度任务必须真的是协程函数。"""

    def test_job_helper_returns_coroutine_function(self):
        """_job() 返回的对象必须能被 APScheduler 识别为协程函数。"""
        from apscheduler.util import iscoroutinefunction_partial

        from app.services.scheduler import _job

        async def dummy():
            return None

        runner = _job("test", dummy)
        assert iscoroutinefunction_partial(runner), (
            "_job() 必须返回协程函数，否则 APScheduler 会把它丢到线程池，"
            "协程对象从未被 await，任务根本不会执行"
        )

    def test_all_registered_jobs_are_coroutines(self):
        """所有 add_job 注册的 job.func 都必须是协程函数。

        直接扫源码找 add_job 的第一个参数，防止以后有人又写回 lambda。
        """
        from apscheduler.util import iscoroutinefunction_partial

        source = (ROOT / "app" / "services" / "scheduler.py").read_text("utf-8")
        tree = ast.parse(source)

        offenders: list[str] = []
        checked = 0

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not (isinstance(func, ast.Attribute) and func.attr == "add_job"):
                continue
            if not node.args:
                continue

            checked += 1
            first_arg = node.args[0]
            # lambda: ... 直接判死刑
            if isinstance(first_arg, ast.Lambda):
                offenders.append(
                    f"第 {first_arg.lineno} 行：add_job 用了 lambda"
                )

        assert checked > 0, "没有找到任何 add_job 调用，扫描器失效了"
        assert not offenders, "add_job 必须用 _job() 包装：" + "; ".join(offenders)

    def test_lambda_would_not_be_detected(self):
        """守住这个测试的前提：lambda 确实不被识别为协程函数。

        如果哪天 APScheduler 改了行为，这个断言会提醒我们重新评估。
        """
        from apscheduler.util import iscoroutinefunction_partial

        async def dummy():
            return None

        assert iscoroutinefunction_partial(dummy) is True
        assert iscoroutinefunction_partial(lambda: dummy()) is False


class TestAsyncContextManagerUsage:
    """同步上下文管理器不能用在 `async with` 里。"""

    def test_no_tenant_filter_async_exists(self):
        """异步版本必须存在。"""
        from app.core.tenant import no_tenant_filter_async

        assert inspect.isasyncgenfunction(
            no_tenant_filter_async.__wrapped__
        ) or hasattr(no_tenant_filter_async, "__wrapped__")

    @pytest.mark.asyncio
    async def test_async_version_works_with_async_with(self):
        """`async with no_tenant_filter_async()` 必须能正常进入和退出。"""
        from app.core.tenant import _tenant_bypass, no_tenant_filter_async

        assert _tenant_bypass.get() is False
        async with no_tenant_filter_async():
            assert _tenant_bypass.get() is True
        assert _tenant_bypass.get() is False

    @pytest.mark.asyncio
    async def test_async_version_restores_on_exception(self):
        """异常路径也必须恢复隔离状态。"""
        from app.core.tenant import _tenant_bypass, no_tenant_filter_async

        with pytest.raises(ValueError):
            async with no_tenant_filter_async():
                raise ValueError("boom")
        assert _tenant_bypass.get() is False

    def test_no_sync_cm_in_async_with(self):
        """扫源码：async with 里不得出现同步的租户上下文管理器。

        这类错误只有运行到那一行才会炸，而且常常被后台框架的
        except 吞掉——所以必须靠静态检查拦。
        """
        sync_managers = {"no_tenant_filter", "as_tenant"}
        offenders: list[str] = []

        for path in (ROOT / "app").rglob("*.py"):
            tree = ast.parse(path.read_text("utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.AsyncWith):
                    continue
                for item in node.items:
                    expr = item.context_expr
                    if isinstance(expr, ast.Call) and isinstance(expr.func, ast.Name):
                        if expr.func.id in sync_managers:
                            offenders.append(
                                f"{path.relative_to(ROOT)}:{expr.lineno} "
                                f"async with {expr.func.id}()"
                            )
                    elif isinstance(expr, ast.Name) and expr.id in sync_managers:
                        offenders.append(
                            f"{path.relative_to(ROOT)}:{expr.lineno} "
                            f"async with {expr.id}"
                        )

        assert not offenders, (
            "同步租户上下文管理器不能用在 async with 里："
            + "; ".join(offenders)
        )


class TestManualTriggerBypassesCron:
    """手动「立即抓取」必须绕过 cron 频率门槛。"""

    def test_crawl_all_pending_accepts_force(self):
        """crawl_all_pending 必须支持 force 参数。"""
        from app.services.crawl_service import crawl_all_pending

        sig = inspect.signature(crawl_all_pending)
        assert "force" in sig.parameters, "手动触发需要 force 参数绕过 cron"

    @pytest.mark.asyncio
    async def test_force_skips_cron_gate(self, monkeypatch):
        """force=True 时，即使 cron 没到点也要执行。

        复现真实故障：用户 15:57 点「立即抓取」，
        但 cron 是「每天 4 点」，should_run_now 返回 False，
        于是点了跟没点一样。
        """
        from app.services import crawl_service

        executed: list[int] = []

        async def fake_crawl_user_sites(db_factory, user_id, force=False):
            executed.append(user_id)
            return {"crawled": 1, "new_posts": 1}

        monkeypatch.setattr(
            crawl_service, "_crawl_user_sites", fake_crawl_user_sites
        )
        monkeypatch.setattr(
            crawl_service, "_mark_crawled",
            lambda user_id, when: _noop(),
        )

        class _FakeResult:
            def all(self):
                return [(1, None, None)]

        class _FakeSession:
            async def execute(self, stmt):
                return _FakeResult()

            async def commit(self):
                return None

        @asynccontextmanager
        async def fake_scope():
            yield _FakeSession()

        monkeypatch.setattr(crawl_service, "session_scope", fake_scope)
        monkeypatch.setattr(crawl_service, "as_tenant_async", _fake_tenant)

        # 不 force：cron 门槛拦下，不执行
        result = await crawl_service.crawl_all_pending()
        assert executed == [], "没到点的用户不该被抓"
        assert result["executed"] == 0

        # force：无视 cron，立即执行
        result = await crawl_service.crawl_all_pending(force=True)
        assert executed == [1], "手动触发必须绕过 cron 门槛"
        assert result["executed"] == 1
        assert result["forced"] is True


async def _noop():
    return None


@asynccontextmanager
async def _fake_tenant(user_id):
    yield
