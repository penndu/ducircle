"""Web 后台页面路由。

页面层与 API 层分离：页面渲染走 Jinja2，数据变更走 JSON API，
两者共用同一套认证依赖和 ORM 租户过滤。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request, status
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import func, select

from app.api.deps import client_ip, validate_registration
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
from app.core.zh_sort import to_pinyin
from app.web import (
    nav_counts,
    render,
    require_page_admin,
    require_page_user,
)
from app.core.sql_compat import desc_nulls_last

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
        # 平台不提供免费额度且用户没填自己的 Key —— 概览页据此引导去设置页
        "key_required": (
            not user.has_own_ai_key and limit <= 0 and user.ai_enabled
        ),
    }


async def _dashboard_insights(db) -> dict:
    """概览页专属数据：趋势、最新文章、抓取健康、活跃友链。

    统一用 Post.crawled_at 做时间轴而不是 created——created 是对方
    文章的发布时间，可能缺失甚至写错，用来画「最近收录」会失真。
    所有查询都走当前请求的会话，租户过滤由 ORM 事件自动注入。
    """
    from datetime import datetime, timedelta

    today = datetime.now().date()
    start_14 = datetime.combine(today - timedelta(days=13), datetime.min.time())
    start_7 = datetime.combine(today - timedelta(days=6), datetime.min.time())

    # 近 14 天每天收录几篇。MySQL 的 DATE() 返回字符串、SQLite 也是，
    # 但驱动可能给 date 对象，统一裁前 10 位最省心。
    raw_days = (
        await db.execute(
            select(func.date(Post.crawled_at), func.count(Post.id))
            .where(Post.deleted_at.is_(None), Post.crawled_at >= start_14)
            .group_by(func.date(Post.crawled_at))
        )
    ).all()
    day_counts = {str(day)[:10]: int(count) for day, count in raw_days}

    trend = []
    for back in range(13, -1, -1):
        day = today - timedelta(days=back)
        trend.append(
            {
                "label": f"{day.month}/{day.day}",
                "iso": day.isoformat(),
                "count": day_counts.get(day.isoformat(), 0),
            }
        )
    peak = max((row["count"] for row in trend), default=0) or 1

    new_7d = (
        await db.execute(
            select(func.count(Post.id)).where(
                Post.deleted_at.is_(None), Post.crawled_at >= start_7
            )
        )
    ).scalar_one()

    post_rows = (
        await db.execute(
            select(
                Post.title,
                Post.link,
                Post.created,
                Post.crawled_at,
                Post.summary,
                Post.ai_score,
                Site.name,
                Site.url,
                Site.avatar,
            )
            .join(Site, Post.site_id == Site.id)
            .where(Post.deleted_at.is_(None))
            .order_by(desc_nulls_last(Post.created), Post.id.desc())
            .limit(6)
        )
    ).all()
    recent_posts = [
        {
            "title": title,
            "link": link,
            "when": created or crawled_at,
            "summary": summary,
            "score": score,
            "site_name": site_name,
            "site_url": site_url,
            "avatar": avatar,
        }
        for title, link, created, crawled_at, summary, score, site_name, site_url, avatar in post_rows
    ]

    log_rows = (
        await db.execute(
            select(CrawlLog.status, func.count(CrawlLog.id))
            .where(CrawlLog.created_at >= start_7)
            .group_by(CrawlLog.status)
        )
    ).all()
    recent_runs = sum(int(count) for _, count in log_rows)
    recent_failed = sum(
        int(count) for status, count in log_rows if status == "failed"
    )

    top_rows = (
        await db.execute(
            select(
                Site.name, Site.url, Site.avatar, func.count(Post.id)
            )
            .join(Post, Post.site_id == Site.id)
            .where(Post.deleted_at.is_(None), Site.deleted_at.is_(None))
            .group_by(Site.id, Site.name, Site.url, Site.avatar)
            .order_by(func.count(Post.id).desc())
            .limit(5)
        )
    ).all()
    top_sites = [
        {"name": n, "url": u, "avatar": a, "count": int(c)}
        for n, u, a, c in top_rows
    ]

    failed_rows = (
        await db.execute(
            select(
                Site.name, Site.url, Site.error_message,
                Site.consecutive_failures,
            )
            .where(Site.error.is_(True))
            .order_by(Site.consecutive_failures.desc())
            .limit(3)
        )
    ).all()
    failed_sites = [
        {"name": n, "url": u, "message": m, "failures": f}
        for n, u, m, f in failed_rows
    ]

    return {
        "trend": trend,
        "trend_peak": peak,
        "new_7d": int(new_7d),
        "recent_posts": recent_posts,
        "recent_runs": recent_runs,
        "recent_failed": recent_failed,
        "top_sites": top_sites,
        "failed_sites": failed_sites,
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
        insights = await _dashboard_insights(db)

    ctx = await _page_context(request, user, counts, "dashboard")
    ctx.update(
        stats=stats,
        strategy_rows=strategy_rows,
        last_crawled=last_site.last_crawled_at if last_site else None,
        ai=ai_state,
        insights=insights,
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
    stmt = stmt.order_by(Site.error.desc())

    rows = (await db.execute(stmt)).scalars().all()
    # 应用层按拼音排，理由见 app/api/sites.py 的 list_sites
    rows = sorted(rows, key=lambda x: (not x.error, to_pinyin(x.name)))

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
        .order_by(desc_nulls_last(Post.created), Post.id.desc())
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
    from app.core.session import NEW_KEY_COOKIE

    ctx = await _page_context(request, user, await nav_counts(db), "settings")
    from app.core.cron import CRON_PRESETS, describe_cron
    from app.services.llm import PROVIDER_DISPLAY_NAMES

    settings = get_settings()

    # 注册后的一次性 Key：只在这一次请求里展示，展示完立刻删掉 Cookie。
    # 数据库只存哈希，不这样做的话用户永远看不到自己的明文 Key。
    new_key = request.cookies.get(NEW_KEY_COOKIE, "")

    ctx.update(
        quota=user.quota_limit(settings.ai_daily_quota),
        providers=list(PROVIDER_DISPLAY_NAMES),
        provider_names=PROVIDER_DISPLAY_NAMES,
        # 采集频率预设
        cron_presets=CRON_PRESETS,
        cron_preset_values=[v for _, v, _ in CRON_PRESETS],
        # 平台默认频率：用户不填就按这个跑，得让人看得见。
        default_crawl_cron=settings.crawl_cron,
        default_crawl_cron_desc=describe_cron(settings.crawl_cron),
        default_link_cron=settings.link_page_cron,
        default_link_cron_desc=describe_cron(settings.link_page_cron),
        allow_registration=settings.allow_registration,
        api_key_display=new_key,
        api_key_is_new=bool(new_key),
    )
    response = render(request, "settings.html", ctx)
    if new_key:
        response.delete_cookie(NEW_KEY_COOKIE, path="/web")
    return response


@router.get("/link-pages", include_in_schema=False)
async def link_pages_page(
    request: Request,
    db: DbSessionDep,
    user: Annotated[User, Depends(require_page_user)],
) -> Response:
    """友链页管理。"""
    from app.models import LinkPage

    pages = (
        await db.execute(
            select(LinkPage)
            .where(LinkPage.user_id == user.id)
            .order_by(LinkPage.created_at.desc())
        )
    ).scalars().all()

    ctx = await _page_context(request, user, await nav_counts(db), "link-pages")
    ctx.update(link_pages=pages)
    return render(request, "link_pages.html", ctx)


# ---- 平台管理 ----


@router.get("/admin/users", include_in_schema=False)
async def admin_users_page(
    request: Request,
    db: DbSessionDep,
    user: Annotated[User, Depends(require_page_admin)],
    search: str | None = None,
    status_filter: str = Query(
        default="all", alias="status", pattern="^(all|active|disabled)$"
    ),
    role: str = Query(default="all", pattern="^(all|admin|user)$"),
    page: int = Query(default=1, ge=1),
) -> Response:
    """平台用户管理。仅管理员可见。

    查询本体复用 ``app.api.admin_users.query_users``——
    页面和 JSON 接口必须用同一套筛选逻辑，否则「搜出来的」
    和「翻页后的」会对不上。
    """
    from app.api.admin_users import platform_overview, query_users

    page_size = 25
    result = await query_users(
        db,
        search=search,
        status_filter=status_filter,
        role=role,
        limit=page_size,
        offset=(page - 1) * page_size,
    )
    overview = await platform_overview(user, db)

    ctx = await _page_context(
        request, user, await nav_counts(db), "admin-users"
    )
    ctx.update(
        overview=overview,
        users=result.items,
        total=result.total,
        page=page,
        page_size=page_size,
        total_pages=max(1, (result.total + page_size - 1) // page_size),
        search=search or "",
        status_filter=status_filter,
        role=role,
        # 前端据此判断「这一行是不是自己」，自己的停用/降权按钮要禁用
        me_id=user.id,
    )
    return render(request, "admin_users.html", ctx)


# ---- 认证页面 ----


@router.get("/login", include_in_schema=False)
async def login_page(
    request: Request,
    db: DbSessionDep,
    next: str = "/web",
) -> Response:
    from app.core.config import get_settings

    return render(
        request,
        "auth.html",
        {
            "mode": "login",
            "next": next,
            "allow_registration": get_settings().allow_registration,
        },
    )


@router.get("/register", include_in_schema=False)
async def register_page(request: Request, db: DbSessionDep) -> Response:
    from app.core.config import get_settings

    return render(
        request,
        "auth.html",
        {
            "mode": "register",
            "allow_registration": get_settings().allow_registration,
        },
    )


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
    user.last_login_ip = client_ip(request)
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
    from app.core.config import get_settings
    from app.core.session import NEW_KEY_COOKIE, NEW_KEY_COOKIE_MAX_AGE

    settings = get_settings()
    username = username.strip()

    if not settings.allow_registration:
        return render(
            request,
            "auth.html",
            {
                "mode": "register",
                "allow_registration": False,
                "error": "平台暂不开放自助注册，请联系管理员开通账号",
                "form_data": {"username": username, "email": email},
            },
            status_code=status.HTTP_403_FORBIDDEN,
        )

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
                "allow_registration": True,
                "error": error,
                "form_data": {"username": username, "email": email},
            },
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    # 第一个账号自动成为管理员，否则全新部署没有人能进管理页
    user_count = (await db.execute(select(func.count(User.id)))).scalar_one()

    api_key = generate_api_key()
    user = User(
        username=username,
        password_hash=hash_password(password),
        email=email.strip() or None,
        api_key_hash=hash_api_key(api_key),
        is_admin=int(user_count) == 0,
        last_login_ip=client_ip(request),
    )
    db.add(user)
    await db.commit()

    # 跳设置页而不是概览页：API Key 只在注册这一刻能拿到，
    # 直接跳概览会让新用户永远看不到自己的 Key，只能去点「重新生成」。
    resp = RedirectResponse("/web/settings", status_code=status.HTTP_303_SEE_OTHER)
    resp.set_cookie(
        SESSION_COOKIE, write_session(user.id), **session_cookie_kwargs()
    )
    resp.set_cookie(
        NEW_KEY_COOKIE,
        api_key,
        max_age=NEW_KEY_COOKIE_MAX_AGE,
        httponly=True,
        samesite="lax",
        secure=settings.base_url.startswith("https://"),
        path="/web",
    )
    return resp





@router.post("/logout", include_in_schema=False)
async def logout() -> Response:
    resp = RedirectResponse("/web/login", status_code=status.HTTP_303_SEE_OTHER)
    resp.delete_cookie(SESSION_COOKIE, path="/")
    return resp