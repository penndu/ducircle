"""AI 模式分流：平台 AI（扣次数） vs 自定义 AI（自带 Key）。

约束：
1. 两种模式选哪桶额度必须互斥——platform 不碰自带 Key，custom 不扣平台；
2. custom 模式没 Key 时必须明确提示，不能悄悄回落到平台；
3. 切换模式与 Key 的保存要走校验后的用户接口；
4. 迁移按存量 Key 回填，升级前后行为一致。
"""

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.models import Base, Post, Site, User
from app.models.user import AI_MODE_CUSTOM, AI_MODE_PLATFORM, AI_MODES
from app.services.ai_summary import AIKeyRequired, quota_state


def _make_engine():
    return create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )


class _OkClient:
    model = "test"

    async def complete(self, *a, **kw):
        return '{"summary": "摘要", "score": 8.0, "reason": "还行"}'


async def _seed(factory, *, mode, key, credits=0):
    async with factory() as db:
        user = User(
            username=f"u_{mode}_{bool(key)}_{credits}",
            password_hash="x",
            ai_mode=mode,
            ai_api_key=key,
            ai_summary_credits=credits,
        )
        db.add(user)
        await db.flush()
        site = Site(user_id=user.id, name="s", url="https://x.com", url_hash=f"h{user.id}")
        db.add(site)
        await db.flush()
        post = Post(
            user_id=user.id,
            site_id=site.id,
            title="标题",
            link=f"https://x.com/p/{user.id}/",
            guid_hash=f"g{user.id}",
            raw_content="<p>" + "正文内容。" * 100 + "</p>",
        )
        db.add(post)
        await db.flush()
        uid, pid = user.id, post.id
        await db.commit()
        return uid, pid


@pytest.mark.asyncio
async def test_mode_constants():
    assert AI_MODES == (AI_MODE_PLATFORM, AI_MODE_CUSTOM)
    engine = _make_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as db:
        u = User(username="u", password_hash="x")
        db.add(u)
        await db.flush()
        assert u.ai_mode == AI_MODE_PLATFORM
    await engine.dispose()


@pytest.mark.asyncio
async def test_platform_mode_ignores_stored_key():
    """平台模式即使库里存着 Key，也必须走兑换余额。"""
    engine = _make_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    uid, _ = await _seed(factory, mode=AI_MODE_PLATFORM, key="sk-should-not-use", credits=3)

    async with factory() as db:
        user = await db.get(User, uid)
        state = await quota_state(user, db)
        assert state.source == "credit", "平台模式不得使用自带 Key"
        assert state.credits == 3

    await engine.dispose()


@pytest.mark.asyncio
async def test_custom_mode_without_key_is_blocked():
    """custom 模式没填 Key -> none，绝不能悄悄回落到平台额度。"""
    engine = _make_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    uid, pid = await _seed(factory, mode=AI_MODE_CUSTOM, key=None, credits=10)

    import app.services.ai_summary as mod

    async with factory() as db:
        user = await db.get(User, uid)
        post = await db.get(Post, pid)
        state = await quota_state(user, db)
        assert not state.allowed
        with pytest.raises(AIKeyRequired) as exc:
            await mod.summarize_post(post, user, db)
        assert "自定义" in str(exc.value)
        assert user.ai_summary_credits == 10, "回落平台会扣余额，必须避免"

    await engine.dispose()


@pytest.mark.asyncio
async def test_custom_mode_does_not_consume_credit():
    engine = _make_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    uid, pid = await _seed(factory, mode=AI_MODE_CUSTOM, key="sk-own", credits=3)

    import app.services.ai_summary as mod

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(mod, "_build_client", lambda u, s: (_OkClient(), "test"))
    try:
        async with factory() as db:
            user = await db.get(User, uid)
            post = await db.get(Post, pid)
            result = await mod.summarize_post(post, user, db)
            await db.commit()
            assert result is not None
            assert user.ai_summary_credits == 3
    finally:
        monkeypatch.undo()

    await engine.dispose()


@pytest.mark.asyncio
async def test_platform_mode_consumes_credit():
    engine = _make_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    uid, pid = await _seed(factory, mode=AI_MODE_PLATFORM, key=None, credits=3)

    import app.services.ai_summary as mod
    from app.core.config import Settings

    settings = Settings()
    settings.ai_platform_api_key = "sk-test-platform"
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(mod, "get_settings", lambda: settings)
    monkeypatch.setattr(mod, "_build_client", lambda u, s: (_OkClient(), "test"))
    try:
        async with factory() as db:
            user = await db.get(User, uid)
            post = await db.get(Post, pid)
            await mod.summarize_post(post, user, db)
            await db.commit()
            assert user.ai_summary_credits == 2
    finally:
        monkeypatch.undo()

    await engine.dispose()


@pytest.mark.asyncio
async def test_build_client_respects_mode():
    from app.core.config import Settings
    from app.services.ai_summary import _build_client

    settings = Settings()
    settings.ai_platform_provider = "siliconflow"
    settings.ai_platform_model = "platform-model"

    platform_user = User(
        username="p", password_hash="x", ai_mode=AI_MODE_PLATFORM,
        ai_api_key="sk-own", ai_provider="minimax_cn", ai_model="own-model",
    )
    client, provider = _build_client(platform_user, settings)
    assert provider == "siliconflow"
    assert client.model == "platform-model"

    custom_user = User(
        username="c", password_hash="x", ai_mode=AI_MODE_CUSTOM,
        ai_api_key="sk-own", ai_provider="minimax_cn", ai_model="own-model",
    )
    client, provider = _build_client(custom_user, settings)
    assert provider == "minimax_cn"
    assert client.model == "own-model"


@pytest.mark.asyncio
async def test_platform_mode_without_server_key_reports_clear_reason(monkeypatch):
    """平台没配服务端 Key 时：给出可读提示，且不扣用户兑换次数。"""
    from app.core.config import Settings
    import app.services.ai_summary as mod

    engine = _make_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    uid, pid = await _seed(factory, mode=AI_MODE_PLATFORM, key=None, credits=5)

    settings = Settings()
    settings.ai_platform_api_key = ""
    monkeypatch.setattr(mod, "get_settings", lambda: settings)

    reasons: list[str] = []
    try:
        async with factory() as db:
            user = await db.get(User, uid)
            post = await db.get(Post, pid)
            result = await mod.summarize_post(post, user, db, skip_reason=reasons)
            assert result is None
            assert reasons, "应给出跳过原因"
            assert "服务端 Key" in reasons[0], reasons[0]
            assert "Illegal header" not in reasons[0], reasons[0]
            assert user.ai_summary_credits == 5, "配置缺失不该扣次数"
    finally:
        monkeypatch.undo()
        await engine.dispose()


@pytest.mark.asyncio
async def test_llm_client_rejects_empty_key_before_network():
    """空 Key 必须抛可读错误，而不是让 httpx 报非法请求头。"""
    from app.services.llm import LLMClient, LLMError

    client = LLMClient(provider="siliconflow", api_key="", model="m")
    with pytest.raises(LLMError, match="API Key 未配置"):
        await client.complete("test")
