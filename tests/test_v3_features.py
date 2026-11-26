"""v3.0.0 两项改动的回归测试。

1. 管理员中心：用户管理 / 兑换码管理收进一个入口；
2. 摘要模式：自动（定时任务）/ 手动（文章页勾选批量生成）。

重点守三条边界：
- 手动模式的用户不能被定时任务扫到；
- 手动批量只能生成自己库里的文章；
- 旧的管理页地址仍然可用，收藏夹不能坏。
"""

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool


@pytest_asyncio.fixture
async def env(monkeypatch):
    import app.db as db_module
    from app.core.ratelimit import RateLimiter
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

    async def _allow(self, user_id, cost=1.0, rate=None, burst=None):
        return True

    monkeypatch.setattr(RateLimiter, "allow", _allow)

    from app.main import app as fastapi_app

    clients = {}

    def make_client():
        c = AsyncClient(
            transport=ASGITransport(app=fastapi_app), base_url="http://test"
        )
        clients[id(c)] = c
        return c

    yield factory, make_client

    for c in clients.values():
        await c.aclose()
    await engine.dispose()


async def _register(c, username):
    return await c.post(
        "/web/register",
        data={"username": username, "password": "password123", "email": ""},
        follow_redirects=False,
    )


async def _seed_post(factory, user_id, title="标题"):
    from app.models import Post, Site

    async with factory() as db:
        site = Site(
            user_id=user_id,
            name="站点",
            url=f"https://blog.example/{user_id}/",
            url_hash=f"site-{user_id}-{title}",
        )
        db.add(site)
        await db.flush()
        post = Post(
            user_id=user_id,
            site_id=site.id,
            title=title,
            link=f"https://blog.example/{user_id}/p",
            guid_hash=f"post-{user_id}-{title}",
        )
        db.add(post)
        await db.flush()
        pid = post.id
        await db.commit()
        return pid


async def _set_summary_mode(factory, user_id, mode):
    from app.models import User

    async with factory() as db:
        await db.execute(
            update(User).where(User.id == user_id).values(summary_mode=mode)
        )
        await db.commit()


async def _give_credits(factory, user_id, n=10):
    from app.models import User

    async with factory() as db:
        await db.execute(
            update(User)
            .where(User.id == user_id)
            .values(ai_summary_credits=n, ai_mode="platform")
        )
        await db.commit()


class TestSummaryModeSetting:
    @pytest.mark.asyncio
    async def test_patch_accepts_only_known_modes(self, env):
        factory, make_client = env
        c = make_client()
        await _register(c, "alice")

        r = await c.patch("/api/users/me", json={"summary_mode": "manual"})
        assert r.status_code == 200, r.text
        assert r.json()["summary_mode"] == "manual"

        async with factory() as db:
            from app.models import User

            u = (await db.execute(select(User).where(User.username == "alice"))).scalar_one()
            assert u.summary_mode == "manual"

        r = await c.patch("/api/users/me", json={"summary_mode": "sometimes"})
        assert r.status_code == 400

    @pytest.mark.asyncio
    async def test_default_is_auto(self, env):
        factory, make_client = env
        c = make_client()
        await _register(c, "alice")
        r = await c.patch("/api/users/me", json={"ai_enabled": True})
        assert r.json()["summary_mode"] == "auto"

    @pytest.mark.asyncio
    async def test_settings_page_has_mode_radios(self, env):
        _, make_client = env
        c = make_client()
        await _register(c, "alice")
        html = (await c.get("/web/settings")).text
        assert 'name="summary-mode"' in html
        assert 'value="manual"' in html
        assert 'value="auto"' in html
        assert "摘要触发方式" in html
        assert "saveSummaryMode" in html


class TestCronSkipsManualUsers:
    @pytest.mark.asyncio
    async def test_only_auto_users_are_visited(self, env, monkeypatch):
        factory, make_client = env
        await _register(make_client(), "auto_user")
        await _register(make_client(), "manual_user")
        await _set_summary_mode(factory, 2, "manual")

        import app.services.ai_summary as mod

        called = []

        async def _fake(user_id, limit=10):
            called.append(user_id)
            return {"generated": 0, "skipped": 0, "quota_exceeded": False}

        monkeypatch.setattr(mod, "summarize_pending_for_user", _fake)
        monkeypatch.setattr(
            mod, "get_settings", lambda: type("S", (), {"ai_enabled": True})()
        )

        result = await mod.summarize_all_pending(limit_per_user=5)
        assert called == [1], f"手动模式的用户被定时任务跑了：{called}"
        assert result["users"] == 1


class TestSelectedBatchSummarize:
    @pytest.mark.asyncio
    async def test_only_selected_ids_are_sent_to_worker(self, env, monkeypatch):
        factory, make_client = env
        c = make_client()
        await _register(c, "alice")
        ids = [await _seed_post(factory, 1, f"文章{i}") for i in range(3)]
        await _give_credits(factory, 1)

        import app.services.ai_summary as mod

        captured = {}

        async def _fake(user_id, limit=20, post_ids=None):
            captured.update(user_id=user_id, limit=limit, post_ids=post_ids)
            return {"generated": 0, "skipped": 0}

        monkeypatch.setattr(mod, "summarize_pending_for_user", _fake)

        r = await c.post(
            "/api/ai/posts/batch-summarize", json={"post_ids": [ids[0], ids[2]]}
        )
        assert r.status_code == 200, r.text
        assert captured["user_id"] == 1
        assert captured["limit"] == 2
        assert captured["post_ids"] == [ids[0], ids[2]]

    @pytest.mark.asyncio
    async def test_foreign_post_is_rejected(self, env):
        factory, make_client = env
        alice, bob = make_client(), make_client()
        await _register(alice, "alice")
        await _register(bob, "bob")
        bob_post = await _seed_post(factory, 2, "Bob 的私密文章")
        await _give_credits(factory, 1)

        r = await alice.post(
            "/api/ai/posts/batch-summarize", json={"post_ids": [bob_post]}
        )
        assert r.status_code == 404, r.text

    @pytest.mark.asyncio
    async def test_empty_selection_and_wrong_type_rejected(self, env):
        _, make_client = env
        c = make_client()
        await _register(c, "alice")
        assert (
            await c.post("/api/ai/posts/batch-summarize", json={"post_ids": []})
        ).status_code == 422

    @pytest.mark.asyncio
    async def test_posts_page_has_batch_controls(self, env):
        factory, make_client = env
        c = make_client()
        await _register(c, "alice")
        await _seed_post(factory, 1)
        html = (await c.get("/web/posts")).text
        assert "批量生成摘要" in html
        assert 'id="pick-all"' in html
        # 文章流改为「全部文章可勾选」（批量删除也需要），类名由
        # summary-pick 改为 post-pick，筛选标签同时上线。
        assert "post-pick" in html
        assert "/api/ai/posts/batch-summarize" in html

        await _set_summary_mode(factory, 1, "manual")
        html = (await c.get("/web/posts")).text
        assert "手动模式：勾选后批量生成" in html


class TestAdminCenter:
    @pytest.mark.asyncio
    async def test_admin_entry_redirects_and_shows_both_tabs(self, env):
        _, make_client = env
        c = make_client()
        await _register(c, "alice")

        r = await c.get("/web/admin", follow_redirects=False)
        assert r.status_code == 307
        # v3.2.0 起管理员中心默认落到「总览」
        assert r.headers["location"] == "/web/admin/overview"

        html = (await c.get("/web/admin/users")).text
        assert "/web/admin/overview" in html
        assert "/web/admin/users" in html
        assert "/web/admin/redeem" in html
        assert "/web/admin/ai" in html
        assert "总览" in html
        assert "用户管理" in html
        assert "兑换码管理" in html
        assert "AI 接口设置" in html

        r = await c.get("/web/admin?tab=redeem", follow_redirects=False)
        assert r.headers["location"] == "/web/admin/redeem"

    @pytest.mark.asyncio
    async def test_legacy_admin_urls_still_work(self, env):
        _, make_client = env
        c = make_client()
        await _register(c, "alice")
        assert (await c.get("/web/admin/redeem")).status_code == 200

    @pytest.mark.asyncio
    async def test_regular_user_cannot_enter_admin_center(self, env):
        admin, user = env[1](), env[1]()
        await _register(admin, "alice")
        await _register(user, "bob")
        assert (await user.get("/web/admin")).status_code == 403


class TestPostsSummaryTabsAndBatchDelete:
    """文章流三标签筛选 + 批量删除（v3.4）。"""

    @staticmethod
    async def _seed(factory, user_id, items):
        from app.models import Post, Site

        ids = []
        async with factory() as db:
            site = Site(
                user_id=user_id,
                name="站点",
                url=f"https://tab.example/{user_id}/",
                url_hash=f"tab-site-{user_id}",
            )
            db.add(site)
            await db.flush()
            for i, (title, summary) in enumerate(items):
                p = Post(
                    user_id=user_id, site_id=site.id, title=title,
                    link=f"https://tab.example/{user_id}/{i}",
                    guid_hash=f"tab-{user_id}-{i}", summary=summary,
                )
                db.add(p)
                await db.flush()
                ids.append(p.id)
            await db.commit()
        return ids

    @pytest.mark.asyncio
    async def test_tabs_filter_and_counts(self, env):
        factory, make_client = env
        c = make_client()
        await _register(c, "tabs")
        await self._seed(factory, 1, [
            ("已A", "摘要A"), ("已B", "摘要B"),
            ("空串", ""), ("无A", None), ("无B", None),
        ])

        all_html = (await c.get("/web/posts")).text
        assert "共 5 篇" in all_html
        for label in ("全部文章流", "已摘要文章流", "未摘要文章流"):
            assert label in all_html

        sum_html = (await c.get("/web/posts?summary=summarized")).text
        assert "已A" in sum_html and "已B" in sum_html
        assert "无A" not in sum_html and "空串" not in sum_html
        assert "共 2 篇" in sum_html

        un_html = (await c.get("/web/posts?summary=unsummarized")).text
        assert "无A" in un_html and "无B" in un_html and "空串" in un_html
        assert "已A" not in un_html
        assert "共 3 篇" in un_html
        # 非 all 标签的分页链接必须保留 summary，否则翻页跳回全部
        assert "summary=unsummarized" in un_html

        # 非法值回落 all
        assert "共 5 篇" in (await c.get("/web/posts?summary=xxx")).text

    @pytest.mark.asyncio
    async def test_batch_delete_syncs_counts_and_isolates_tenant(self, env):
        factory, make_client = env
        c = make_client()
        await _register(c, "deluser")
        ids = await self._seed(factory, 1, [("A", None), ("B", None), ("C", "摘要C")])

        r = await c.post("/api/posts/batch-delete", json={"post_ids": ids[:2]})
        assert r.status_code == 200, r.text
        assert r.json()["deleted"] == 2
        assert "共 1 篇" in (await c.get("/web/posts")).text

        # 参数校验
        empty = await c.post("/api/posts/batch-delete", json={"post_ids": []})
        too_many = await c.post(
            "/api/posts/batch-delete", json={"post_ids": list(range(51))}
        )
        assert empty.status_code == 422
        assert too_many.status_code == 422

        # 租户隔离：删别人的文章查不到
        other = make_client()
        await _register(other, "other")
        foreign_ids = await self._seed(factory, 2, [("别人的", None)])
        r2 = await c.post("/api/posts/batch-delete", json={"post_ids": foreign_ids})
        assert r2.status_code == 200
        assert r2.json()["deleted"] == 0
