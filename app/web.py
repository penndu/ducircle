"""Jinja2 模板环境与全局上下文。"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Annotated

from fastapi import Depends, Request
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user_session
from app.core.config import get_settings
from app.db import get_db
from app.models import Post, Site, User

BASE_DIR = Path(__file__).resolve().parent.parent
TEMPLATE_DIR = BASE_DIR / "web" / "templates"

templates = Jinja2Templates(directory=str(TEMPLATE_DIR))
templates.env.globals["settings"] = get_settings()


def _time_ago(value) -> str:
    """相对时间。中文用户看「3天前」比看时间戳直观得多。"""
    if not isinstance(value, datetime):
        return "从未"
    delta = datetime.now() - value
    seconds = delta.total_seconds()
    if seconds < 60:
        return "刚刚"
    if seconds < 3600:
        return f"{int(seconds // 60)} 分钟前"
    if seconds < 86400:
        return f"{int(seconds // 3600)} 小时前"
    if seconds < 86400 * 30:
        return f"{int(seconds // 86400)} 天前"
    if seconds < 86400 * 365:
        return f"{int(seconds // (86400 * 30))} 个月前"
    return f"{int(seconds // (86400 * 365))} 年前"


def _truncate(value, n: int = 40) -> str:
    if not value:
        return ""
    return value[:n] + "…" if len(value) > n else value


# filter 必须在函数定义之后注册，否则会 NameError
templates.env.filters["datetime"] = lambda v: (
    v.strftime("%Y-%m-%d %H:%M") if isinstance(v, datetime) else "-"
)
templates.env.filters["date"] = lambda v: (
    v.strftime("%Y-%m-%d") if isinstance(v, datetime) else "-"
)
templates.env.filters["ago"] = _time_ago
templates.env.filters["truncate_title"] = _truncate


async def get_optional_user(
    request: Request, db: Annotated[AsyncSession, Depends(get_db)]
) -> User | None:
    """页面级可选登录。未登录返回 None 而不是抛 401——
    登录页和首页需要能匿名访问。

    注意：这里的 db 与路由函数的 db 是同��个 Depends(get_db)，
    FastAPI 对同一依赖默认缓存（use_cache=True），因此拿到的是同一个会话。
    """
    from app.core.session import SESSION_COOKIE, read_session

    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    user_id = read_session(token)
    if user_id is None:
        return None

    user = (
        await db.execute(select(User).where(User.id == user_id))
    ).scalar_one_or_none()
    if user is None or not user.is_active:
        return None

    from app.core.tenant import set_tenant

    set_tenant(user.id)
    return user


async def require_page_user(
    user: Annotated[User | None, Depends(get_optional_user)],
) -> User:
    """页面级强制登录。返回 401 并带 Location 头指���登录页。"""
    from fastapi import HTTPException, status

    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="请先登录",
            headers={"Location": "/web/login"},
        )
    return user


async def nav_counts(db: AsyncSession) -> dict:
    """导航栏上的计数，让用户一眼看到有多少站点出问题。"""
    sites = (await db.execute(select(func.count(Site.id)))).scalar_one()
    errors = (
        await db.execute(
            select(func.count(Site.id)).where(Site.error.is_(True))
        )
    ).scalar_one()
    posts = (
        await db.execute(
            select(func.count(Post.id)).where(Post.deleted_at.is_(None))
        )
    ).scalar_one()
    return {"sites": sites, "errors": errors, "posts": posts}


def render(
    request: Request,
    name: str,
    context: dict | None = None,
    status_code: int = 200,
):
    """统一渲染入口，自动注入 request 和 app_name。

    status_code 单独作为参数，不混进模板上下文——
    否则 Jinja2 会把它当成模板变量去渲染，抛「unhashable type: dict」。
    """
    ctx = {
        "request": request,
        "app_name": get_settings().app_name,
        **(context or {}),
    }
    return templates.TemplateResponse(
        request, name, ctx, status_code=status_code
    )