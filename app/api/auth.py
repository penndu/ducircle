"""注册、登录、API Key 管理。"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status

from pydantic import BaseModel, Field
from sqlalchemy import func, select

from app.api.deps import CurrentUserSession, DbSession, validate_registration
from app.core.security import (
    generate_api_key,
    hash_api_key,
    hash_password,
    valid_username,
    verify_password,
)
from app.core.session import SESSION_COOKIE, session_cookie_kwargs, write_session
from app.models import Post, Site, User

router = APIRouter(prefix="/api/auth", tags=["认证"])


class RegisterRequest(BaseModel):
    username: str = Field(min_length=3, max_length=32)
    password: str = Field(min_length=8, max_length=128)
    email: str | None = None


class LoginRequest(BaseModel):
    username: str
    password: str


class UserOut(BaseModel):
    id: int
    username: str
    email: str | None
    is_admin: bool
    ai_enabled: bool
    ai_provider: str | None
    has_own_ai_key: bool
    created_at: datetime


class RegisterResponse(UserOut):
    api_key: str = Field(description="只显示这一次，数据库里只存哈希")


class KeyResponse(BaseModel):
    api_key: str


@router.post(
    "/register",
    response_model=RegisterResponse,
    status_code=status.HTTP_201_CREATED,
    summary="注册账号",
)
async def register(payload: RegisterRequest, db: DbSession) -> RegisterResponse:
    """注册新账号。

    开放注册意味着可能被批量注册刷号，因此：
    - 用户名唯一索引挡重复
    - 资源创建走限流中间件（见 app/api/ratelimit_middleware.py）
    """
    validate_registration(payload.username, payload.password)

    existing = (
        await db.execute(
            select(User).where(func.lower(User.username) == payload.username.lower())
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(status_code=409, detail="用户名已被占用")

    api_key = generate_api_key()
    user = User(
        username=payload.username,
        password_hash=hash_password(payload.password),
        email=payload.email,
        api_key_hash=hash_api_key(api_key),
    )
    db.add(user)
    await db.flush()

    return RegisterResponse(
        id=user.id,
        username=user.username,
        email=user.email,
        is_admin=user.is_admin,
        ai_enabled=user.ai_enabled,
        ai_provider=user.ai_provider,
        has_own_ai_key=False,
        created_at=user.created_at,
        api_key=api_key,
    )


@router.post("/login", summary="登录")
async def login(payload: LoginRequest, response: Response, db: DbSession) -> dict:
    user = (
        await db.execute(
            select(User).where(func.lower(User.username) == payload.username.lower())
        )
    ).scalar_one_or_none()

    # 用户不存在与密码错误返回同一条消息，避免用户名枚举
    if user is None or not verify_password(payload.password, user.password_hash):
        raise HTTPException(status_code=401, detail="用户名或密码错误")

    if not user.is_active:
        raise HTTPException(status_code=403, detail="账号已被禁用")

    user.last_login_at = datetime.now()
    token = write_session(user.id)
    response.set_cookie(
        SESSION_COOKIE, token, **session_cookie_kwargs()
    )
    return {"ok": True, "username": user.username}


@router.post("/logout", summary="退出登录")
async def logout(response: Response) -> dict:
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"ok": True}


@router.get("/me", response_model=UserOut, summary="当前用户信息")
async def me(user: CurrentUserSession) -> UserOut:
    return UserOut(
        id=user.id,
        username=user.username,
        email=user.email,
        is_admin=user.is_admin,
        ai_enabled=user.ai_enabled,
        ai_provider=user.ai_provider,
        has_own_ai_key=user.has_own_ai_key,
        created_at=user.created_at,
    )


@router.post(
    "/api-key/regenerate",
    response_model=KeyResponse,
    summary="重新生成 API Key",
)
async def regenerate_key(user: CurrentUserSession, db: DbSession) -> KeyResponse:
    """重新生成会立即作废旧 Key——博客前端需要同步更新配置。"""
    api_key = generate_api_key()
    user.api_key_hash = hash_api_key(api_key)
    await db.flush()
    return KeyResponse(api_key=api_key)


@router.get("/stats", summary="我的数据概览")
async def my_stats(user: CurrentUserSession, db: DbSession) -> dict:
    sites = (
        await db.execute(select(func.count(Site.id)))
    ).scalar_one()
    posts = (
        await db.execute(select(func.count(Post.id)))
    ).scalar_one()
    return {
        "sites": sites,
        "posts": posts,
        "api_key_hint": (user.api_key_hash or "")[:6],
    }