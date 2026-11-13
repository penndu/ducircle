"""认证与限流依赖。

双轨认证：
- 后台（/web/*）用 Session Cookie，浏览器直接访问
- 只读 API（/v1/*）用 API Key，给博客前端和其他站调用

两者都最终调用 set_tenant() 把用户身份写进 ContextVar，
后续所有 ORM 查询自动带上 user_id 过滤。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Header, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import hash_api_key, valid_username, verify_api_key
from app.core.session import SESSION_COOKIE, read_session_data
from app.core.tenant import set_tenant
from app.db import get_db
from app.models import User


async def get_current_user_session(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db)],
) -> User:
    """从 Session Cookie 解析用户。用于后台页面与后台接口。"""
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="未登录"
        )

    data = read_session_data(token)
    if data is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="登录已过期"
        )
    user_id = data["uid"]

    user = (
        await session.execute(select(User).where(User.id == user_id))
    ).scalar_one_or_none()
    if user is None or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="账号不可用"
        )

    # 改过密码后，旧会话一律作废。
    # Session 是无状态签名，没法主动踢人下线——
    # 只能靠「签发时间早于改密时间」这个判断把旧 Cookie 挡在门外。
    #
    # ⚠️ 用 `<=` 而不是 `<`：改密接口换发的新会话签发于同一秒，
    # 用 `<` 会把新会话也判成旧的、当场失效。
    # 所以规则是：签发时间必须**严格大于**改密时刻才算有效。
    changed_at = user.password_changed_at
    if changed_at is not None:
        issued_at = data.get("iat")
        if isinstance(issued_at, int) and issued_at <= changed_at:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="密码已修改，请重新登录",
            )

    set_tenant(user.id)
    return user


async def get_current_user_key(
    session: Annotated[AsyncSession, Depends(get_db)],
    authorization: Annotated[str | None, Header()] = None,
    x_api_key: Annotated[str | None, Header(alias="X-API-Key")] = None,
) -> User:
    """从 API Key 解析用户。用于只读接口。

    接受两种传法：Authorization: Bearer <key> 或 X-API-Key: <key>。
    后者对前端更友好，前者是事实标准，两者都支持最省事。
    """
    key = x_api_key
    if not key and authorization:
        if authorization.lower().startswith("bearer "):
            key = authorization[7:].strip()
        else:
            key = authorization.strip()

    if not key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="缺少 API Key，请用 X-API-Key 头或 Authorization: Bearer",
        )

    key_hash = hash_api_key(key)
    user = (
        await session.execute(
            select(User).where(
                User.api_key_hash == key_hash, User.is_active.is_(True)
            )
        )
    ).scalar_one_or_none()

    if user is None or not verify_api_key(key, user.api_key_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="API Key 无效"
        )

    set_tenant(user.id)
    return user


async def require_admin(
    user: Annotated[User, Depends(get_current_user_session)],
) -> User:
    if not user.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="需要管理员权限"
        )
    return user


def validate_registration(username: str, password: str) -> None:
    """注册参数校验。抛 HTTPException 而不是返回错误列表，
    让调用方只需处理一种失败形态。"""
    if not valid_username(username):
        raise HTTPException(
            status_code=400,
            detail="用户名只能包含字母、数字、下划线、点、连字符，长度 3~32",
        )
    if len(password) < 8:
        raise HTTPException(status_code=400, detail="密码至少 8 位")
    if len(password) > 128:
        raise HTTPException(status_code=400, detail="密码过长")


def client_ip(request: Request) -> str | None:
    """真实客户端 IP。

    生产环境前面是 Nginx，``request.client.host`` 永远是 127.0.0.1，
    必须优先读 ``X-Forwarded-For`` 的第一段（代理链最左端才是真实来源）。

    截断到 64 字符：请求头是攻击者可控的，不截断可能撑爆列宽。
    """
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()[:64] or None
    if request.client is not None:
        return request.client.host[:64] or None
    return None


# 常用依赖别名，让路由签名简洁
CurrentUserSession = Annotated[User, Depends(get_current_user_session)]
CurrentUserKey = Annotated[User, Depends(get_current_user_key)]
AdminUser = Annotated[User, Depends(require_admin)]
DbSession = Annotated[AsyncSession, Depends(get_db)]