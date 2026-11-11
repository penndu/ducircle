"""FastAPI 应用入口。"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from app.api.ratelimit_middleware import RateLimitMiddleware, TenantContextMiddleware
from app.core.config import get_settings

settings = get_settings()

BASE_DIR = Path(__file__).resolve().parent.parent

logging.basicConfig(
    level=logging.DEBUG if settings.debug else logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("ducircle")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期：启动时初始化，关闭时释放连接。"""
    logger.info("正在启动 %s", settings.app_name)

    import app.db as db_module
    from app.core.ratelimit import RedisManager
    from app.services.scheduler import shutdown_scheduler, start_scheduler

    if not await db_module.check_db():
        logger.error("数据库连接失败，请检查 MYSQL_* 配置")
        raise RuntimeError("数据库不可用")
    logger.info("数据库连接正常")

    if await RedisManager.instance().ping():
        logger.info("Redis 连接正常")
    else:
        # Redis 挂了限流和锁都失效，但服务本身还能用——
        # 限流降级放行，分布式锁也放行（代价是可能重复抓取）。
        # 不因此拒绝启动：数据库正常就该跑起来。
        logger.warning("Redis 不可用，限流与分布式锁将降级，请尽快修复")

    app.state.scheduler = await start_scheduler()

    yield

    await shutdown_scheduler()
    await RedisManager.instance().close()
    await db_module.dispose_engine()
    logger.info("已关闭")


app = FastAPI(
    title=settings.app_name,
    description="面向博客主的公开友链聚合服务",
    version="0.1.0",
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
)

# 中间件执行顺序是「后注册的在最外层」。
# TenantContext 必须比限流更内层：它负责在每个请求开始时清空租户状态，
# 放最内层才能保证路由处理前后状态都是干净的。
app.add_middleware(RateLimitMiddleware)
app.add_middleware(TenantContextMiddleware)

# ---- 路由 ----
from app.api import admin, ai, auth, export, opml, users, web_pages  # noqa: E402
from app.api import link_pages, posts, sites  # noqa: E402

app.include_router(auth.router)
app.include_router(users.router)
app.include_router(ai.router)
app.include_router(export.router)
app.include_router(opml.router)
app.include_router(web_pages.router)
app.include_router(admin.router)
app.include_router(sites.router)
app.include_router(link_pages.router)
app.include_router(posts.web_router)
app.include_router(posts.v1_router)

# 静态资源
static_dir = BASE_DIR / "web" / "static"
if static_dir.exists():
    app.mount("/static", StaticFiles(directory=static_dir), name="static")


# ---- 基础端点 ----


@app.get("/health", tags=["运维"], summary="健康检查")
async def health() -> dict:
    """供 Docker HEALTHCHECK 和负载均衡探活使用。"""
    import os

    import app.db as db_module

    db_ok = await db_module.check_db()
    return {
        "status": "ok" if db_ok else "degraded",
        "db": "ok" if db_ok else "down",
        "version": app.version,
        # 构建版本，由 CI 通过 --build-arg 注入。
        # 部署后第一件事就是确认跑的是不是预期的那个版本——
        # 部署到 latest 时这一项尤其有用。
        "build": os.environ.get("DUCIRCLE_VERSION", "dev"),
        "built_at": os.environ.get("DUCIRCLE_BUILD_TIME", "unknown"),
    }


async def _public_overview() -> dict:
    """公开首页上的全站数字。

    只取两个总数，不返回任何明细。匿名请求没有租户上下文，
    这里仍显式豁免：将来中间件若给未登录请求绑定默认租户，
    统计也不会突然变成 0。
    查询失败时返回空值，落地页照常渲染——首页不该因为数据库
    抖动就 500。
    """
    from sqlalchemy import func, select

    from app.core.tenant import no_tenant_filter_async
    from app.db import session_scope
    from app.models import Post, Site

    try:
        async with no_tenant_filter_async():
            async with session_scope() as db:
                sites = (
                    await db.execute(
                        select(func.count(Site.id)).where(
                            Site.deleted_at.is_(None)
                        )
                    )
                ).scalar_one()
                posts = (
                    await db.execute(
                        select(func.count(Post.id)).where(
                            Post.deleted_at.is_(None)
                        )
                    )
                ).scalar_one()
        return {"sites": sites, "posts": posts}
    except Exception as exc:  # noqa: BLE001
        logger.warning("公开统计读取失败，首页降级为静态展示: %s", exc)
        return {"sites": None, "posts": None}


@app.get("/", include_in_schema=False)
async def index(request: Request) -> Response:
    """公开落地页。模板自带样式，不依赖 Tailwind CDN。"""
    from app.web import render

    return render(request, "landing.html", {"public": await _public_overview()})


@app.exception_handler(RequestValidationError)
async def validation_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    """把 Pydantic 的校验错误转成人话，别把字段结构直接甩给用户。"""
    errors = []
    for err in exc.errors():
        field = ".".join(str(p) for p in err["loc"] if p not in ("body", "query"))
        errors.append(f"{field or '参数'}: {err['msg']}")
    return JSONResponse(
        status_code=422, content={"detail": "；".join(errors)}
    )