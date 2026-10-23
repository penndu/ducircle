"""Web 后台页面路由。

页面层与 API 层分离：页面渲染走 Jinja2，数据变更走 JSON API，
两者共用同一套认证依赖和 ORM 租户过滤。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request, status
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import func, select

from app.api.deps import validate_registration
from app.core.security import (
    generate_api_key,
    hash_api_key,
    hash_password,
    valid_username,
    verify_password,
)
from app.core.session import SESSION_COOKIE, session_cookie_kwargs, write_session
from app.db import get_db
from sqlalchemy.ext.asyncio import AsyncSession
from app.models import CrawlLog, Post, Site, User
from app.web import nav_counts, render, require_page_user

router = APIRouter(prefix="/web", tags=["后台页面"])

# 关键：别名必须指向 app.db.get_db 本身（同一个函数对象），
# FastAPI 才能按依赖的 callable 做缓存，让页面路由的 db 参数与
# require_page_user 内部用的是同一个会话。
# 若这里重新定义一个 wrapper，FastAPI 会当成两个不同依赖，
# 各自调用 get_db 生成器——路由拿到的就会是未 await 的协程。
DbSessionDep = Annotated[AsyncSession, Depends(get_db)]


async def _ai_state(user: User, db) -> dict:
    """仪表盘展示用的 AI 状态。"""
    from app.core.config import get_settings
    from app.services.ai_summary import check_quota

    settings = get_settings()
    allowed, used, limit = await check_quota(user, db)
    return {
        "enabled": settings.ai_enabled and user.ai_enabled,
        "has_own_key": user.has_own_ai_key,
        "daily_limit": limit,
        "used": used,
        "remaining": None if user.has_own_ai_key else max(0, limit - used),
    }


async def _page_context(request: Request, user: User, counts: dict, page: str) -> dict:
    """所有页面共用的上下文。

    counts 由调用方传入而非在这里查——部分路由（dashboard）
    自己管理会话，生命周期与其它路由不同。
    """
    return {
        "current_user": user,
        "current_page": page,
        "counts": counts,
        # 数据库存的是 API Key 的哈希，无法还原明文。
        # 页面上展示哈希前 8 位作为标识，用户需要明文时走「重新生成」。
        "api_key_hint": (user.api_key_hash or "")[:8],
        "api_key_display": "",
    }


@router.get("", include_in_schema=False)
@router.get("/", include_in_schema=False)
async def dashboard(
    request: Request,
    user: Annotated[User, Depends(require_page_user)],
) -> Response:
    """仪表盘。

    这里刻意不用 Depends(get_db) 注入 db，而是自己开一个会话。
    原因是叠加两个路由装饰器时，FastAPI 会为同一 handler 复用依赖缓存，
    注入的会话被求值一次后再次访问就拿到已关闭对象，
    表现为 `'coroutine' object has no attribute 'scalar_one'`。
    自己管理会话可以彻底绕开这个歧义。
    """
    from app.api.admin import compute_user_stats
    from app.db import session_scope

    async with session_scope() as db:
        stats = await compute_user_stats(db)

        strategy_rows = (
            await db.execute(
                select(Post.strategy, func.count(Post.id))
                .where(Post.deleted_at.is_(None))
                .group_by(Post.strategy)
                .order_by(func.count(Post.id).desc())
            )
        ).all()

        last_site = (
            await db.execute(
                select(Site).order_by(Site.last_crawled_at.desc()).limit(1)
            )
        ).scalar_one_or_none()

        counts = await nav_counts(db)
        ai_state = await _ai_state(user, db)

    ctx = await _page_context(request, user, counts, "dashboard")
    ctx.update(
        stats=stats,
        strategy_rows=strategy_rows,
        last_crawled=last_site.last_crawled_at if last_site else None,
        ai=ai_state,
    )
    return render(request, "dashboard.html", ctx)


@router.get("/sites", include_in_schema=False)
async def sites_page(
    request: Request,
    db: DbSessionDep,
    user: Annotated[User, Depends(require_page_user)],
    filter: Annotated[str, Query(pattern="^(all|error)$")] = "all",
    search: str | None = None,
) -> Response:
    from sqlalchemy import or_

    stmt = select(Site)
    if filter == "error":
        stmt = stmt.where(Site.error.is_(True))
    if search:
        stmt = stmt.where(or_(Site.name.contains(search), Site.url.contains(search)))
    stmt = stmt.order_by(Site.error.desc(), Site.name)

    rows = (await db.execute(stmt)).scalars().all()

    # 附上每站文章数，避免 N+1 查询
    counts = {}
    if rows:
        counts = dict(
            (
                await db.execute(
                    select(Post.site_id, func.count(Post.id))
                    .where(
                        Post.site_id.in_([s.id for s in rows]),
                        Post.deleted_at.is_(None),
                    )
                    .group_by(Post.site_id)
                )
            ).all()
        )

    sites = []
    for s in rows:
        s.post_count = counts.get(s.id, 0)
        sites.append(s)

    ctx = await _page_context(request, user, await nav_counts(db), "sites")
    ctx.update(sites=sites, filter=filter, search=search)
    return render(request, "sites.html", ctx)


@router.get("/posts", include_in_schema=False)
async def posts_page(
    request: Request,
    db: DbSessionDep,
    user: Annotated[User, Depends(require_page_user)],
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 30,
) -> Response:
    from sqlalchemy import or_

    stmt = (
        select(Post, Site.name, Site.url, Site.avatar)
        .join(Site, Post.site_id == Site.id)
        .where(Post.deleted_at.is_(None))
        .order_by(Post.created.desc().nullslast(), Post.id.desc())
        .offset(offset)
        .limit(limit + 1)
    )
    rows = (await db.execute(stmt)).all()

    has_more = len(rows) > limit
    rows = rows[:limit]

    posts = []
    for p, site_name, site_url, avatar in rows:
        p.site_name = site_name
        p.site_url = site_url
        p.avatar = avatar
        posts.append(p)

    ctx = await _page_context(request, user, await nav_counts(db), "posts")
    ctx.update(posts=posts, has_more=has_more, offset=offset, limit=limit)
    return render(request, "posts.html", ctx)


@router.get("/failed", include_in_schema=False)
async def failed_page(
    request: Request,
    db: DbSessionDep,
    user: Annotated[User, Depends(require_page_user)],
) -> Response:
    failed = (
        await db.execute(
            select(Site)
            .where(Site.error.is_(True))
            .order_by(Site.consecutive_failures.desc(), Site.last_crawled_at.desc())
        )
    ).scalars().all()

    logs = (
        await db.execute(
            select(CrawlLog).order_by(CrawlLog.created_at.desc()).limit(30)
        )
    ).scalars().all()

    ctx = await _page_context(request, user, await nav_counts(db), "failed")
    ctx.update(failed=failed, logs=logs)
    return render(request, "failed.html", ctx)


@router.get("/settings", include_in_schema=False)
async def settings_page(
    request: Request,
    db: DbSessionDep,
    user: Annotated[User, Depends(require_page_user)],
) -> Response:
    from app.core.config import get_settings

    ctx = await _page_context(request, user, await nav_counts(db), "settings")
    ctx.update(
        quota=user.quota_limit(get_settings().ai_daily_quota),
        providers=["gemini", "siliconflow", "bigmodel"],
    )
    return render(request, "settings.html", ctx)


# ---- 认证页面 ----


@router.get("/login", include_in_schema=False)
async def login_page(
    request: Request,
    db: DbSessionDep,
    next: str = "/web",
) -> Response:
    return render(
        request,
        "auth.html",
        {"mode": "login", "next": next},
    )


@router.get("/register", include_in_schema=False)
async def register_page(request: Request, db: DbSessionDep) -> Response:
    return render(request, "auth.html", {"mode": "register"})


@router.post("/login", include_in_schema=False)
async def login_submit(
    request: Request,
    db: DbSessionDep,
    username: Annotated[str, Form()],
    password: Annotated[str, Form()],
) -> Response:
    user = (
        await db.execute(
            select(User).where(func.lower(User.username) == username.strip().lower())
        )
    ).scalar_one_or_none()

    if user is None or not verify_password(password, user.password_hash):
        return render(
            request,
            "auth.html",
            {
                "mode": "login",
                "error": "用户名或密码错误",
                "form_data": {"username": username},
            },
            status_code=status.HTTP_401_UNAUTHORIZED,
        )

    if not user.is_active:
        return render(
            request,
            "auth.html",
            {"mode": "login", "error": "账号已被禁用"},
            status_code=status.HTTP_403_FORBIDDEN,
        )

    from datetime import datetime

    user.last_login_at = datetime.now()
    await db.commit()

    resp = RedirectResponse("/web", status_code=status.HTTP_303_SEE_OTHER)
    resp.set_cookie(
        SESSION_COOKIE, write_session(user.id), **session_cookie_kwargs()
    )
    return resp


@router.post("/register", include_in_schema=False)
async def register_submit(
    request: Request,
    db: DbSessionDep,
    username: Annotated[str, Form()],
    password: Annotated[str, Form()],
    email: Annotated[str, Form()] = "",
) -> Response:
    username = username.strip()

    error = None
    try:
        validate_registration(username, password)
    except HTTPException as exc:
        error = exc.detail

    if error is None:
        exists = (
            await db.execute(
                select(User).where(func.lower(User.username) == username.lower())
            )
        ).scalar_one_or_none()
        if exists is not None:
            error = "用户名已被占用"

    if error:
        return render(
            request,
            "auth.html",
            {
                "mode": "register",
                "error": error,
                "form_data": {"username": username, "email": email},
            },
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    api_key = generate_api_key()
    user = User(
        username=username,
        password_hash=hash_password(password),
        email=email.strip() or None,
        api_key_hash=hash_api_key(api_key),
    )
    db.add(user)
    await db.commit()

    resp = RedirectResponse("/web", status_code=status.HTTP_303_SEE_OTHER)
    resp.set_cookie(
        SESSION_COOKIE, write_session(user.id), **session_cookie_kwargs()
    )
    return resp


@router.post("/logout", include_in_schema=False)
async def logout() -> Response:
    resp = RedirectResponse("/web/login", status_code=status.HTTP_303_SEE_OTHER)
    resp.delete_cookie(SESSION_COOKIE, path="/")
    return resp