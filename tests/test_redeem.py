"""兑换码：生成、兑换、扣次数。

这个功能动到钱，所以要守住的东西比一般功能多：

1. **码不能重复发** —— 同一个码并发兑换也只能成功一次（条件 UPDATE + rowcount）。
2. **作废只能作用在未使用的码上** —— 已使用的码作废等于收回用户已付钱的权益。
3. **次数必须真的扣在余额上** —— 扣错了用户会看到「钱花了次数没动」。
4. **免费额度与付费余额不能互相污染** —— 算「今日免费额度用了几篇」时
   不能把兑换来的次数算进去，反过来扣余额时也不能顺手记成免费额度。
5. **管理接口普通用户碰不得** —— 403，不是「页面能打开但空的」。
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

PASSWORD = "password123"


@pytest_asyncio.fixture
async def app_env(monkeypatch):
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

    yield fastapi_app, factory
    await engine.dispose()


@pytest_asyncio.fixture
async def make_client(app_env):
    """按需创建客户端。每个客户端独立 cookie jar ——
    管理员和普通用户必须各用一个，否则 Set-Cookie 会互相覆盖。"""
    app, _ = app_env
    created: list[AsyncClient] = []

    def _make() -> AsyncClient:
        client = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
        created.append(client)
        return client

    yield _make
    for client in created:
        await client.aclose()


async def register(client: AsyncClient, username: str):
    """注册并保持登录（注册接口会写 Session Cookie）。"""
    return await client.post(
        "/web/register",
        data={"username": username, "password": PASSWORD, "email": ""},
        follow_redirects=False,
    )


async def make_batch(admin: AsyncClient, plan: str = "trial", quantity: int = 3):
    r = await admin.post(
        "/api/admin/redeem-codes/batches",
        json={"plan": plan, "quantity": quantity, "note": "测试批次"},
    )
    assert r.status_code == 201, r.text
    return r.json()


# ---- 纯逻辑 ----


class TestPlansAndCodes:
    def test_plan_catalog_matches_business(self):
        """两档的定价和次数是业务约定，写错了就是少收钱或多给货。"""
        from app.services.redeem import PLANS

        assert PLANS["trial"]["credits"] == 100
        assert PLANS["trial"]["price_cents"] == 200
        assert PLANS["standard"]["credits"] == 1000
        assert PLANS["standard"]["price_cents"] == 1000

    def test_code_format(self):
        from app.services.redeem import generate_code

        code = generate_code("trial")
        assert code.startswith("FC2-")
        assert generate_code("standard").startswith("FC10-")
        # FC2-XXXX-XXXX-XXXX-XXXX → 23 字符，列宽 String(32) 装得下
        assert len(code) == 23
        assert len(code.split("-")) == 5

    def test_generated_codes_are_unique(self):
        from app.services.redeem import generate_code

        codes = {generate_code("trial") for _ in range(500)}
        assert len(codes) == 500

    def test_alphabet_avoids_confusable_chars(self):
        """0/O/1/I/L 这类字符手抄容易错，必须不在字母表里。"""
        from app.services.redeem import CODE_ALPHABET

        for ch in "01OIL":
            assert ch not in CODE_ALPHABET

    @pytest.mark.parametrize(
        "raw",
        [
            "FC2-YGRR-VTX2-RGC7-UDQC",
            "fc2-ygrr-vtx2-rgc7-udqc",
            "  FC2-YGRR-VTX2-RGC7-UDQC  ",
            "FC2 YGRR VTX2 RGC7 UDQC",
            "FC2YGRRVTX2RGC7UDQC",  # 连字符被漏掉
        ],
    )
    def test_normalize_accepts_messy_input(self, raw):
        from app.services.redeem import normalize_code

        assert normalize_code(raw) == "FC2-YGRR-VTX2-RGC7-UDQC"

    def test_normalize_keeps_garbage_untouched(self):
        from app.services.redeem import normalize_code

        assert normalize_code("hello world") == "HELLOWORLD"
        assert normalize_code("") == ""

    def test_price_yuan(self):
        from app.services.redeem import price_yuan

        assert price_yuan(200) == "2"
        assert price_yuan(1000) == "10"
        assert price_yuan(1250) == "12.50"


# ---- 管理接口 ----


class TestAdminGenerate:
    @pytest.mark.asyncio
    async def test_generate_batch(self, make_client):
        admin = make_client()
        await register(admin, "admin1")

        data = await make_batch(admin, "standard", 5)
        batch = data["batch"]
        assert batch["plan"] == "standard"
        assert batch["credits"] == 1000
        assert batch["quantity"] == 5
        assert batch["unused"] == 5
        assert batch["face_value_yuan"] == "50"  # 10 元 × 5

        assert len(data["codes"]) == 5
        assert len(set(data["codes"])) == 5
        assert all(c.startswith("FC10-") for c in data["codes"])

    @pytest.mark.asyncio
    async def test_plans_endpoint(self, make_client):
        admin = make_client()
        await register(admin, "admin1")

        r = await admin.get("/api/admin/redeem-codes/plans")
        assert r.status_code == 200
        plans = {p["key"]: p for p in r.json()}
        assert plans["trial"]["summary"] == "2 元 · 100 次 AI 摘要"
        assert plans["standard"]["summary"] == "10 元 · 1000 次 AI 摘要"

    @pytest.mark.asyncio
    async def test_quantity_is_capped(self, make_client):
        admin = make_client()
        await register(admin, "admin1")

        r = await admin.post(
            "/api/admin/redeem-codes/batches",
            json={"plan": "trial", "quantity": 100000},
        )
        assert r.status_code == 422  # pydantic 直接挡掉

    @pytest.mark.asyncio
    async def test_unknown_plan_rejected(self, make_client):
        admin = make_client()
        await register(admin, "admin1")

        r = await admin.post(
            "/api/admin/redeem-codes/batches",
            json={"plan": "free", "quantity": 1},
        )
        assert r.status_code == 400
        assert "类型" in r.json()["detail"]

    @pytest.mark.asyncio
    async def test_normal_user_cannot_generate(self, make_client):
        admin = make_client()
        await register(admin, "admin1")
        user = make_client()
        await register(user, "alice")

        r = await user.post(
            "/api/admin/redeem-codes/batches",
            json={"plan": "trial", "quantity": 1},
        )
        assert r.status_code == 403

        assert (await user.get("/api/admin/redeem-codes/codes")).status_code == 403

    @pytest.mark.asyncio
    async def test_anonymous_rejected(self, make_client):
        client = make_client()
        r = await client.post(
            "/api/admin/redeem-codes/batches",
            json={"plan": "trial", "quantity": 1},
        )
        assert r.status_code == 401

    @pytest.mark.asyncio
    async def test_list_and_export_and_revoke(self, make_client):
        admin = make_client()
        await register(admin, "admin1")
        data = await make_batch(admin, "trial", 3)
        batch_id = data["batch"]["id"]

        listing = await admin.get(f"/api/admin/redeem-codes/codes?batch_id={batch_id}")
        assert listing.status_code == 200
        body = listing.json()
        assert body["total"] == 3
        assert body["counts"] == {"all": 3, "unused": 3, "used": 0, "revoked": 0}

        export = await admin.get(
            f"/api/admin/redeem-codes/export?batch_id={batch_id}"
        )
        assert export.status_code == 200
        text = export.text
        for code in data["codes"]:
            assert code in text

        code_id = body["items"][0]["id"]
        rev = await admin.post(
            f"/api/admin/redeem-codes/codes/{code_id}/revoke",
            json={"reason": "重复生成"},
        )
        assert rev.status_code == 200
        assert rev.json()["status"] == "revoked"

        after = await admin.get(f"/api/admin/redeem-codes/codes?batch_id={batch_id}")
        assert after.json()["counts"]["revoked"] == 1
        assert after.json()["counts"]["unused"] == 2

    @pytest.mark.asyncio
    async def test_revoked_code_leave_export(self, make_client):
        """默认只导出还没被用掉的码，否则买家拿到手就是「已被使用」。"""
        admin = make_client()
        await register(admin, "admin1")
        data = await make_batch(admin, "trial", 2)
        batch_id = data["batch"]["id"]
        listing = await admin.get(f"/api/admin/redeem-codes/codes?batch_id={batch_id}")
        revoked_code = None
        for item in listing.json()["items"]:
            await admin.post(
                f"/api/admin/redeem-codes/codes/{item['id']}/revoke",
                json={"reason": "测试"},
            )
            revoked_code = item["code"]

        export = await admin.get(f"/api/admin/redeem-codes/export?batch_id={batch_id}")
        assert revoked_code not in export.text

    @pytest.mark.asyncio
    async def test_cannot_revoke_used_code(self, make_client):
        admin = make_client()
        await register(admin, "admin1")
        data = await make_batch(admin, "trial", 1)
        code = data["codes"][0]

        user = make_client()
        await register(user, "alice")
        assert (
            await user.post("/api/ai/redeem", json={"code": code})
        ).status_code == 200

        listing = await admin.get(
            f"/api/admin/redeem-codes/codes?batch_id={data['batch']['id']}"
        )
        code_id = listing.json()["items"][0]["id"]
        r = await admin.post(
            f"/api/admin/redeem-codes/codes/{code_id}/revoke", json={}
        )
        assert r.status_code == 400
        assert "已经被使用" in r.json()["detail"]


# ---- 用户兑换 ----


class TestRedeem:
    @pytest.mark.asyncio
    async def test_redeem_adds_credits(self, make_client):
        admin = make_client()
        await register(admin, "admin1")
        data = await make_batch(admin, "trial", 1)

        user = make_client()
        await register(user, "alice")

        r = await user.post("/api/ai/redeem", json={"code": data["codes"][0]})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["credits_added"] == 100
        assert body["credits"] == 100
        assert body["plan_label"] == "体验版"

        quota = (await user.get("/api/ai/quota")).json()
        assert quota["credits"] == 100
        assert quota["key_required"] is False

    @pytest.mark.asyncio
    async def test_credits_stack(self, make_client):
        """2 元 + 10 元 = 1100 次。可叠加是明确的产品约定。"""
        admin = make_client()
        await register(admin, "admin1")
        trial = await make_batch(admin, "trial", 1)
        std = await make_batch(admin, "standard", 1)

        user = make_client()
        await register(user, "alice")

        await user.post("/api/ai/redeem", json={"code": trial["codes"][0]})
        r = await user.post("/api/ai/redeem", json={"code": std["codes"][0]})
        assert r.json()["credits"] == 1100

    @pytest.mark.asyncio
    async def test_code_can_only_be_used_once(self, make_client):
        admin = make_client()
        await register(admin, "admin1")
        data = await make_batch(admin, "trial", 1)
        code = data["codes"][0]

        alice = make_client()
        await register(alice, "alice")
        bob = make_client()
        await register(bob, "bob2")

        assert (await alice.post("/api/ai/redeem", json={"code": code})).status_code == 200
        second = await bob.post("/api/ai/redeem", json={"code": code})
        assert second.status_code == 400
        assert "已经被使用" in second.json()["detail"]
        # bob 一次都没拿到
        assert (await bob.get("/api/ai/quota")).json()["credits"] == 0

    @pytest.mark.asyncio
    async def test_unknown_code_rejected(self, make_client):
        user = make_client()
        await register(user, "alice")

        r = await user.post("/api/ai/redeem", json={"code": "FC2-AAAA-BBBB-CCCC-DDDD"})
        assert r.status_code == 400
        assert "无效" in r.json()["detail"]

    @pytest.mark.asyncio
    async def test_revoked_code_rejected(self, make_client):
        admin = make_client()
        await register(admin, "admin1")
        data = await make_batch(admin, "trial", 1)
        batch_id = data["batch"]["id"]
        listing = await admin.get(f"/api/admin/redeem-codes/codes?batch_id={batch_id}")
        await admin.post(
            f"/api/admin/redeem-codes/codes/{listing.json()['items'][0]['id']}/revoke",
            json={"reason": "作废测试"},
        )

        user = make_client()
        await register(user, "alice")
        r = await user.post("/api/ai/redeem", json={"code": data["codes"][0]})
        assert r.status_code == 400
        assert "作废" in r.json()["detail"]

    @pytest.mark.asyncio
    async def test_anonymous_cannot_redeem(self, make_client):
        client = make_client()
        r = await client.post("/api/ai/redeem", json={"code": "FC2-AAAA-BBBB-CCCC-DDDD"})
        assert r.status_code == 401

    @pytest.mark.asyncio
    async def test_redeem_page_renders(self, make_client):
        admin = make_client()
        await register(admin, "admin1")
        data = await make_batch(admin, "trial", 1)

        user = make_client()
        await register(user, "alice")
        await user.post("/api/ai/redeem", json={"code": data["codes"][0]})

        r = await user.get("/web/redeem")
        assert r.status_code == 200, r.text[:400]
        html = r.text
        assert "兑换 AI 摘要次数" in html
        assert "100" in html           # 余额
        assert "体验版" in html        # 兑换记录
        assert "2 元 · 100 次 AI 摘要" in html  # 定价目录

    @pytest.mark.asyncio
    async def test_settings_page_has_redeem_box(self, make_client):
        user = make_client()
        await register(user, "alice")
        html = (await user.get("/web/settings")).text
        assert "兑换次数余额" in html
        assert "/web/redeem" in html


# ---- 额度扣减 ----


async def _seed_post(factory, *, own_key: str | None, credits: int):
    from app.models import Post, Site, User

    async with factory() as db:
        user = User(
            username=f"u_{own_key or 'none'}_{credits}",
            password_hash="x",
            ai_api_key=own_key,
            ai_summary_credits=credits,
        )
        db.add(user)
        await db.flush()
        site = Site(user_id=user.id, name="s", url="https://x.com", url_hash="h")
        db.add(site)
        await db.flush()
        post = Post(
            user_id=user.id,
            site_id=site.id,
            title="标题",
            link="https://x.com/p/1/",
            guid_hash="g1",
            raw_content="<p>" + "正文内容。" * 100 + "</p>",
        )
        db.add(post)
        await db.flush()
        uid, pid = user.id, post.id
        await db.commit()
    return uid, pid


class _OkClient:
    model = "test-model"

    async def complete(self, *args, **kwargs):
        return '{"summary": "一段摘要", "score": 8.5, "reason": "有点东西"}'


@pytest.mark.asyncio
async def test_summary_consumes_credit(app_env, monkeypatch):
    """没 Key 但有余额：生成一篇扣 1 次，且记成「兑换余额」而不是免费额度。"""
    _, factory = app_env
    uid, pid = await _seed_post(factory, own_key=None, credits=3)

    import app.services.ai_summary as mod

    monkeypatch.setattr(mod, "_build_client", lambda u, s: (_OkClient(), "test"))

    from sqlalchemy import select

    from app.models import AIQuotaLog, User

    async with factory() as db:
        user = await db.get(User, uid)
        post = await db.get(mod.Post, pid)
        result = await mod.summarize_post(post, user, db)
        await db.commit()

    assert result is not None
    async with factory() as db:
        user = await db.get(User, uid)
        assert user.ai_summary_credits == 2
        log = (
            await db.execute(select(AIQuotaLog).where(AIQuotaLog.user_id == uid))
        ).scalars().all()
        assert len(log) == 1
        assert log[0].from_credit is True
        assert log[0].from_user_key is False


@pytest.mark.asyncio
async def test_own_key_does_not_consume_credit(app_env, monkeypatch):
    """自带 Key 的账号不受次数限制，余额一动不动。"""
    _, factory = app_env
    uid, pid = await _seed_post(factory, own_key="sk-test", credits=3)

    import app.services.ai_summary as mod

    monkeypatch.setattr(mod, "_build_client", lambda u, s: (_OkClient(), "test"))

    from sqlalchemy import select

    from app.models import AIQuotaLog, User

    async with factory() as db:
        user = await db.get(User, uid)
        post = await db.get(mod.Post, pid)
        await mod.summarize_post(post, user, db)
        await db.commit()

    async with factory() as db:
        user = await db.get(User, uid)
        assert user.ai_summary_credits == 3
        log = (
            await db.execute(select(AIQuotaLog).where(AIQuotaLog.user_id == uid))
        ).scalars().all()
        assert log[0].from_user_key is True
        assert log[0].from_credit is False


@pytest.mark.asyncio
async def test_no_key_no_credit_raises(app_env, monkeypatch):
    """既没 Key 又没余额：报 AIKeyRequired，且提示里要提到兑换码这条出路。"""
    _, factory = app_env
    uid, pid = await _seed_post(factory, own_key=None, credits=0)

    import app.services.ai_summary as mod

    async with factory() as db:
        user = await db.get(mod.User, uid)
        post = await db.get(mod.Post, pid)
        with pytest.raises(mod.AIKeyRequired) as exc:
            await mod.summarize_post(post, user, db)

    assert "兑换码" in str(exc.value)


@pytest.mark.asyncio
async def test_batch_allowed_with_credit(make_client):
    """有余额时批量生成不该被 403 拦下。"""
    admin = make_client()
    await register(admin, "admin1")
    data = await make_batch(admin, "trial", 1)

    user = make_client()
    await register(user, "alice")
    await user.post("/api/ai/redeem", json={"code": data["codes"][0]})

    r = await user.post("/api/ai/batch?limit=2")
    assert r.status_code == 200, r.text


# ---- 管理页与人工调整 ----


class TestAdminPageAndManualAdjust:
    @pytest.mark.asyncio
    async def test_admin_redeem_page(self, make_client):
        admin = make_client()
        await register(admin, "admin1")
        await make_batch(admin, "standard", 2)

        r = await admin.get("/web/admin/redeem")
        assert r.status_code == 200, r.text[:400]
        html = r.text
        assert "兑换码生成器" in html
        assert "10 元 · 1000 次 AI 摘要" in html
        assert "最近的批次" in html

    @pytest.mark.asyncio
    async def test_normal_user_gets_403_on_admin_page(self, make_client):
        admin = make_client()
        await register(admin, "admin1")
        user = make_client()
        await register(user, "alice")
        r = await user.get("/web/admin/redeem")
        assert r.status_code == 403

    @pytest.mark.asyncio
    async def test_admin_can_top_up_credits(self, make_client):
        """补发、退款、纠错要能手工改，不用为此再造一张码。"""
        admin = make_client()
        await register(admin, "admin1")
        user = make_client()
        await register(user, "alice")

        listing = await admin.get("/api/admin/users")
        alice = next(u for u in listing.json()["items"] if u["username"] == "alice")
        assert alice["ai_summary_credits"] == 0

        r = await admin.patch(
            f"/api/admin/users/{alice['id']}", json={"ai_summary_credits": 250}
        )
        assert r.status_code == 200
        assert r.json()["ai_summary_credits"] == 250

        quota = (await user.get("/api/ai/quota")).json()
        assert quota["credits"] == 250
