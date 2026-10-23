"""Session 签发与校验。

用 itsdangerous 做签名 Session，而不是服务端存储：
后台用户量小（几百人），无状态更省事，也省一张表。
代价是不能主动踢人下线——公开服务规模下这个代价可以接受。
"""

from __future__ import annotations

import time

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from app.core.config import get_settings

# Session 有效期 7 天
MAX_AGE = 7 * 24 * 3600

# Session Cookie 名。放这里而不是 deps.py——cookie 的读写方都需要它，
# 放在核心模块里避免认证层与路由层互相导入。
SESSION_COOKIE = "ducircle_session"

_serializer: URLSafeTimedSerializer | None = None


def _get_serializer() -> URLSafeTimedSerializer:
    global _serializer
    if _serializer is None:
        settings = get_settings()
        _serializer = URLSafeTimedSerializer(
            settings.secret_key, salt="ducircle-session"
        )
    return _serializer


def write_session(user_id: int) -> str:
    """签发 Session token，内含用户 ID 与签发时间。"""
    return _get_serializer().dumps({"uid": user_id, "iat": int(time.time())})


def read_session(token: str) -> int | None:
    """校验并解析 Session token。无效或过期返回 None。"""
    try:
        data = _get_serializer().loads(token, max_age=MAX_AGE)
    except (BadSignature, SignatureExpired):
        return None
    except Exception:  # noqa: BLE001
        return None

    uid = data.get("uid") if isinstance(data, dict) else None
    return uid if isinstance(uid, int) else None


def session_cookie_kwargs() -> dict:
    """Session Cookie 的安全属性。

    httponly 让 JS 读不到，降低 XSS 借 cookie 越权的风险；
    samesite=lax 防 CSRF——后台表单是 POST，跨站提交会被拦。
    """
    settings = get_settings()
    is_https = settings.base_url.startswith("https://")
    return {
        "httponly": True,
        "samesite": "lax",
        "secure": is_https,
        "max_age": MAX_AGE,
        "path": "/",
    }