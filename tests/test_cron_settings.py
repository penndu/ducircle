"""采集频率设置测试。

覆盖三层：
1. cron 工具函数——校验、描述、预设
2. API——保存、校验非法表达式、权限隔离
3. 页面——渲染、下拉框选项、保存按钮
"""

from __future__ import annotations

from datetime import datetime

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.core.cron import (
    CRON_PRESETS,
    describe_cron,
    user_cron_or_default,
    validate_cron,
)


class TestValidateCron:
    def test_empty_is_valid(self):
        """空表示「用平台默认」，是合法的。"""
        assert validate_cron("").valid
        assert validate_cron(None).valid
        assert validate_cron("   ").valid

    def test_valid_expressions(self):
        for _, expr, _ in CRON_PRESETS:
            result = validate_cron(expr)
            assert result.valid, f"{expr} 应该合法：{result.error}"

    def test_wrong_field_count(self):
        result = validate_cron("0 3 * *")
        assert not result.valid
        assert "5 段" in result.error

    def test_invalid_values(self):
        result = validate_cron("99 99 * * *")
        assert not result.valid

    def test_garbage(self):
        result = validate_cron("abc")
        assert not result.valid

    def test_error_message_is_human(self):
        """错误提示要说人话，不是抛异常堆栈。"""
        result = validate_cron("0 3 * *")
        assert result.error
        assert "Traceback" not in result.error


class TestDescribeCron:
    def test_every_minute(self):
        assert "分钟" in describe_cron("*/30 * * * *")

    def test_every_n_hours(self):
        assert describe_cron("0 */6 * * *") == "每 6 小时"

    def test_daily(self):
        assert describe_cron("0 8 * * *") == "每天 8 点"

    def test_twice_daily(self):
        assert "和" in describe_cron("0 6,18 * * *")

    def test_weekly(self):
        assert "每周" in describe_cron("0 8 * * 1")

    def test_invalid_returns_empty(self):
        assert describe_cron("bad") == ""


class TestUserCronOrDefault:
    def test_none_uses_default(self):
        assert user_cron_or_default(None, "0 * * * *") == "0 * * * *"

    def test_empty_uses_default(self):
        assert user_cron_or_default("", "0 * * * *") == "0 * * * *"

    def test_invalid_uses_default(self):
        """非法表达式不能进调度器——用默认值兜底。"""
        assert user_cron_or_default("garbage", "0 * * * *") == "0 * * * *"

    def test_valid_is_kept(self):
        assert user_cron_or_default("0 */4 * * *", "0 * * * *") == "0 */4 * * *"


@pytest_asyncio.fixture
async def client_with_app(monkeypatch):
    import app.db as db_module
    from app.models import Base

    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(db_module, "get_session_factory", lambda: factory)

    async def _check_db():
        return True

    monkeypatch.setattr(db_module, "check_db", _check_db)

    from app.core.ratelimit import RateLimiter

    async def _allow(self, user_id, cost=1.0, rate=None, burst=None):
        return True

    monkeypatch.setattr(RateLimiter, "allow", _allow)

    from app.services.scheduler import start_scheduler

    async def _noop_start():
        return None

    monkeypatch.setattr("app.main.start_scheduler", _noop_start, raising=False)

    from app.main import app as fastapi_app

    transport = ASGITransport(app=fastapi_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client, factory

    await engine.dispose()


async def register_and_login(client, username="cron_user"):
    await client.post(
        "/web/register",
        data={"username": username, "password": "password123", "email": ""},
        follow_redirects=False,
    )
    r = await client.post(
        "/api/auth/login",
        json={"username": username, "password": "password123"},
    )
    assert r.status_code == 200


class TestCronApi:
    @pytest.mark.asyncio
    async def test_save_cron(self, client_with_app):
        client, _ = client_with_app
        await register_and_login(client)

        r = await client.patch(
            "/api/users/me",
            json={"crawl_cron": "0 */4 * * *", "link_page_cron": "0 5 * * *"},
        )
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["crawl_cron"] == "0 */4 * * *"
        assert data["link_page_cron"] == "0 5 * * *"

    @pytest.mark.asyncio
    async def test_reject_invalid_cron(self, client_with_app):
        """非法表达式必须被拒绝，不能进库。"""
        client, _ = client_with_app
        await register_and_login(client)

        r = await client.patch("/api/users/me", json={"crawl_cron": "garbage"})
        assert r.status_code == 400
        assert "无效" in r.json()["detail"]

    @pytest.mark.asyncio
    async def test_reject_wrong_field_count(self, client_with_app):
        client, _ = client_with_app
        await register_and_login(client)

        r = await client.patch("/api/users/me", json={"link_page_cron": "0 3 * *"})
        assert r.status_code == 400

    @pytest.mark.asyncio
    async def test_empty_resets_to_default(self, client_with_app):
        """传空字符串等于「用平台默认」，应该存成 NULL。"""
        client, _ = client_with_app
        await register_and_login(client)

        await client.patch("/api/users/me", json={"crawl_cron": "0 */4 * * *"})
        r = await client.patch("/api/users/me", json={"crawl_cron": ""})
        assert r.status_code == 200
        assert r.json()["crawl_cron"] is None

    @pytest.mark.asyncio
    async def test_presets_endpoint(self, client_with_app):
        client, _ = client_with_app
        await register_and_login(client)

        r = await client.get("/api/users/me/cron-presets")
        assert r.status_code == 200
        presets = r.json()["presets"]
        assert len(presets) > 0
        assert all("label" in p and "value" in p for p in presets)

    @pytest.mark.asyncio
    async def test_requires_login(self, client_with_app):
        client, _ = client_with_app
        r = await client.patch("/api/users/me", json={"crawl_cron": "0 * * * *"})
        assert r.status_code == 401


class TestSettingsPage:
    @pytest.mark.asyncio
    async def test_page_renders_with_cron_section(self, client_with_app):
        client, _ = client_with_app
        await register_and_login(client)

        r = await client.get("/web/settings")
        assert r.status_code == 200
        assert "采集频率" in r.text
        assert "文章采集频率" in r.text
        assert "友链采集频率" in r.text

    @pytest.mark.asyncio
    async def test_page_has_save_button(self, client_with_app):
        """要有保存按钮。"""
        client, _ = client_with_app
        await register_and_login(client)

        r = await client.get("/web/settings")
        assert r.status_code == 200
        assert "saveCron" in r.text
        assert "保存" in r.text

    @pytest.mark.asyncio
    async def test_page_shows_presets(self, client_with_app):
        """下拉框里要有预设选项。"""
        client, _ = client_with_app
        await register_and_login(client)

        r = await client.get("/web/settings")
        assert r.status_code == 200
        assert "每 6 小时" in r.text
        assert "每天 凌晨 3 点" in r.text

    @pytest.mark.asyncio
    async def test_page_shows_current_value(self, client_with_app):
        """已保存的值要回显。"""
        client, _ = client_with_app
        await register_and_login(client)

        await client.patch("/api/users/me", json={"crawl_cron": "0 */4 * * *"})

        r = await client.get("/web/settings")
        assert r.status_code == 200
        assert "0 */4 * * *" in r.text


class TestCronMatching:
    """cron 表达式的匹配逻辑。

    自己实现了匹配（没用 APScheduler 的 get_next_fire_time），
    所以必须测透——调度器的行为全靠它。
    """

    def test_matches_exact_minute(self):
        from app.core.cron import cron_matches

        moment = datetime(2026, 10, 24, 12, 0)
        assert cron_matches("0 * * * *", moment)
        assert not cron_matches("30 * * * *", moment)

    def test_matches_step(self):
        """*/N 的基准是该字段的最小值。"""
        from app.core.cron import cron_matches

        # 小时字段：0,6,12,18
        assert cron_matches("0 */6 * * *", datetime(2026, 10, 24, 12, 0))
        assert cron_matches("0 */6 * * *", datetime(2026, 10, 24, 18, 0))
        assert not cron_matches("0 */6 * * *", datetime(2026, 10, 24, 13, 0))

        # 分字段：0,30
        assert cron_matches("*/30 * * * *", datetime(2026, 10, 24, 12, 30))
        assert not cron_matches("*/30 * * * *", datetime(2026, 10, 24, 12, 15))

    def test_matches_enumeration(self):
        """枚举字段要逐项试，不能第一个不匹配就放弃。"""
        from app.core.cron import cron_matches

        assert cron_matches("0 6,18 * * *", datetime(2026, 10, 24, 6, 0))
        assert cron_matches("0 6,18 * * *", datetime(2026, 10, 24, 18, 0))
        assert not cron_matches("0 6,18 * * *", datetime(2026, 10, 24, 12, 0))

    def test_matches_range(self):
        from app.core.cron import cron_matches

        assert cron_matches("0 9-18 * * *", datetime(2026, 10, 24, 12, 0))
        assert not cron_matches("0 9-18 * * *", datetime(2026, 10, 24, 20, 0))

    def test_matches_weekday(self):
        """2026-10-24 是周六，cron 里周六是 6。"""
        from app.core.cron import cron_matches

        assert cron_matches("0 8 * * 6", datetime(2026, 10, 24, 8, 0))
        assert not cron_matches("0 8 * * 1", datetime(2026, 10, 24, 8, 0))

    def test_sunday_is_zero_or_seven(self):
        from app.core.cron import cron_matches

        sunday = datetime(2026, 10, 25, 8, 0)
        assert cron_matches("0 8 * * 0", sunday)
        assert cron_matches("0 8 * * 7", sunday)

    def test_day_and_week_are_or(self):
        """cron 规定日和周是「或」关系。"""
        from app.core.cron import cron_matches

        # 15 号是每月一次，周六也匹配
        assert cron_matches("0 8 15 * 6", datetime(2026, 10, 24, 8, 0))


class TestShouldRunNow:
    """「到点了没」的判断——用户级频率的核心。"""

    def test_runs_when_trigger_after_last_run(self):
        from app.core.cron import should_run_now

        now = datetime(2026, 10, 24, 12, 8)
        # 上次 11:58 跑的，12:00 的触发点晚于它 -> 该跑
        assert should_run_now("0 * * * *", datetime(2026, 10, 24, 11, 58), now)

    def test_skips_when_already_run(self):
        from app.core.cron import should_run_now

        now = datetime(2026, 10, 24, 12, 8)
        # 上次 12:03 跑的，12:00 已经处理过了 -> 不该跑
        assert not should_run_now("0 * * * *", datetime(2026, 10, 24, 12, 3), now)

    def test_first_run_only_if_recent(self):
        """第一次执行不能把过去一天的活全补跑。"""
        from app.core.cron import should_run_now

        # 刚到点 -> 跑
        assert should_run_now("0 * * * *", None, datetime(2026, 10, 24, 12, 0))
        # 很久以前 -> 不跑
        assert not should_run_now(
            "0 * * * *", None, datetime(2026, 10, 24, 15, 30)
        )

    def test_different_frequencies(self):
        from app.core.cron import should_run_now

        now = datetime(2026, 10, 24, 13, 8)
        last = datetime(2026, 10, 24, 11, 58)
        # 每 6 小时：12:00 的触发点晚于 11:58 -> 跑
        assert should_run_now("0 */6 * * *", last, now)
        # 每 12 小时：12:00 触发点也晚于 11:58 -> 跑
        assert should_run_now("0 */12 * * *", last, now)
        # 每天 8 点：今天 8 点早于 11:58 -> 不跑
        assert not should_run_now("0 8 * * *", last, now)

    def test_uses_default_when_user_has_none(self):
        from app.core.cron import should_run_now

        now = datetime(2026, 10, 24, 12, 8)
        last = datetime(2026, 10, 24, 11, 58)
        assert should_run_now(None, last, now, default="0 * * * *")
        assert not should_run_now(None, datetime(2026, 10, 24, 12, 3), now,
                                 default="0 * * * *")
