"""平台取消免费 AI 额度后的行为约束。

三件事必须成立：
1. 平台默认额度为 0，没填自己的 Key 就生成不了摘要；
2. 报错要说「去填 Key」，而不是「额度用完，明天再来」；
3. 设置页要让人看见平台默认的采集频率是多少。
"""

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool


@pytest_asyncio.fixture
async def client(monkeypatch):
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

    from app.main import app as fastapi_app

    transport = ASGITransport(app=fastapi_app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    await engine.dispose()


async def _register(client, username="alice"):
    return await client.post(
        "/web/register",
        data={"username": username, "password": "password123", "email": ""},
        follow_redirects=False,
    )


class TestNoFreeQuota:
    def test_platform_daily_quota_is_zero(self):
        from app.core.config import get_settings

        assert get_settings().ai_daily_quota == 0

    def test_quota_override_zero_wins(self):
        """override=0 必须生效，不能被当成「未设置」。"""
        from app.models import User

        user = User(id=1, username="u", password_hash="x",
                    ai_daily_quota_override=0)
        assert user.quota_limit(10) == 0

        user.ai_daily_quota_override = None
        assert user.quota_limit(10) == 10

    def test_key_required_exception_is_distinct(self):
        """AIKeyRequired 与 QuotaExceeded 分开，提示语不同。"""
        from app.services.ai_summary import AIKeyRequired, QuotaExceeded

        assert AIKeyRequired is not QuotaExceeded
        assert not issubclass(AIKeyRequired, QuotaExceeded)


class TestSettingsPageCopy:
    @pytest.mark.asyncio
    async def test_settings_requires_own_key(self, client):
        await _register(client)
        html = (await client.get("/web/settings")).text
        assert "需要你自己的 API Key" in html
        assert "平台不再提供免费摘要额度" in html
        # 不该再出现「留空则用平台额度」这种旧文案
        assert "留空则用平台额度" not in html
        assert "当前使用平台额度" not in html

    @pytest.mark.asyncio
    async def test_settings_shows_platform_default_cron(self, client):
        """设置页要写明平台默认频率。"""
        await _register(client)
        html = (await client.get("/web/settings")).text
        assert "平台默认频率" in html

        from app.core.config import get_settings

        settings = get_settings()
        assert settings.crawl_cron in html
        assert settings.link_page_cron in html

    @pytest.mark.asyncio
    async def test_cron_default_human_readable(self, client):
        """平台默认频率要有人话描述，不能只丢一串 cron。"""
        from app.core.config import get_settings
        from app.core.cron import describe_cron

        settings = get_settings()
        assert describe_cron(settings.crawl_cron), "默认采集频率应能翻译成人话"
        assert describe_cron(settings.link_page_cron)

    def test_describe_cron_handles_multi_hour(self):
        """平台默认的 '0 0,6,12,18 * * *' 要能翻译。"""
        from app.core.cron import describe_cron

        text = describe_cron("0 0,6,12,18 * * *")
        assert text, "多钟点表达式必须能描述出来"
        assert "每天" in text
        assert "0" in text and "6" in text


class TestDashboardCopy:
    @pytest.mark.asyncio
    async def test_dashboard_guides_to_fill_key(self, client):
        await _register(client)
        html = (await client.get("/web")).text
        assert "平台不提供免费摘要额度" in html
        assert "去填写 API Key" in html
        assert "/web/settings" in html

    @pytest.mark.asyncio
    async def test_quota_api_reports_key_required(self, client):
        await _register(client)
        r = await client.get("/api/ai/quota")
        assert r.status_code == 200
        data = r.json()
        assert data["key_required"] is True
        assert data["daily_limit"] == 0

    @pytest.mark.asyncio
    async def test_batch_summary_rejected_without_key(self, client):
        """没填 Key 时批量生成要立刻拒绝，而不是跑完才失败。"""
        await _register(client)
        r = await client.post("/api/ai/batch?limit=5")
        assert r.status_code == 403
        assert "API Key" in r.json()["detail"]
