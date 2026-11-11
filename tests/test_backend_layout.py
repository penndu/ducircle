"""后台布局的回归测试。

后台内容区之前锁死在 max-w-5xl（1024px），宽屏下右侧空出
一大块，页面看着像没做完。这里用测试把这个约束钉住。
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


class TestBackendLayout:
    def test_layout_content_area_is_not_narrow_capped(self):
        """内容区不能锁死在 max-w-5xl（1024px）——宽屏下会空掉一大块。"""
        import re
        from pathlib import Path

        layout = Path("web/templates/layout.html").read_text(encoding="utf-8")
        # 只看 class 属性里的值，注释里提到旧类名不算
        classes = " ".join(re.findall(r'class="([^"]*)"', layout))
        assert "max-w-5xl" not in classes, "内容区又退回了 1024px 窄版"
        assert "max-w-[1560px]" in classes

    def test_list_pages_use_two_column_grid(self):
        """四个列表页在宽屏下要双列，否则卡片被拉得过宽、行内稀疏。"""
        from pathlib import Path

        for name in ("sites.html", "posts.html", "link_pages.html", "failed.html"):
            html = Path(f"web/templates/{name}").read_text(encoding="utf-8")
            assert "list-grid" in html, f"{name} 没切到双列栅格"

    def test_settings_page_stays_narrow(self):
        """设置页是表单，宽度受限才不至于一行拉太长。"""
        from pathlib import Path

        html = Path("web/templates/settings.html").read_text(encoding="utf-8")
        assert "form-narrow" in html

    @pytest.mark.asyncio
    async def test_dashboard_uses_two_column_main_grid(self, client):
        await _register(client)
        r = await client.get("/web")
        assert r.status_code == 200, r.text[:400]
        html = r.text
        assert "ds-grid" in html
        assert "ds-col-main" in html
        assert "ds-col-side" in html
        # 策略分布与最活跃友链并排，不再纵向堆在右栏
        assert "ds-pair" in html
        # 右栏补了快捷入口，不再是四张卡就结束
        assert "快捷操作" in html

    @pytest.mark.asyncio
    async def test_dashboard_keeps_existing_sections(self, client):
        """改版不能把原有信息块弄丢。"""
        await _register(client)
        html = (await client.get("/web")).text
        for text in [
            "近 14 天收录趋势", "最新文章", "抓取健康", "AI 摘要",
            "API Key", "抓取策略分布", "最活跃的友链", "快捷操作",
        ]:
            assert text in html, f"概览页缺少「{text}」"
