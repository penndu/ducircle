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


def write_session(user_id: int, issued_at: int | None = None) -> str:
    """签发 Session token，内含用户 ID 与签发时间。

    issued_at 可以指定签发时刻——改密码后要换发新会话时，
    用它保证新会话的签发时间**严格晚于**改密时刻，
    否则会被「旧会话作废」的检查当成旧会话挡掉。
    """
    return _get_serializer().dumps(
        {"uid": user_id, "iat": issued_at if issued_at is not None else int(time.time())}
    )


def read_session(token: str) -> int | None:
    """校验并解析 Session token。无效或过期返回 None。"""
    data = read_session_data(token)
    if data is None:
        return None
    return data.get("uid")


def read_session_data(token: str) -> dict | None:
    """解析 Session token，返回完整内容（含签发时间）。

    改密码后要作废旧会话，光有 uid 不够——还需要签发时间
    才能和 password_changed_at 比较。
    """
    try:
        data = _get_serializer().loads(token, max_age=MAX_AGE)
    except (BadSignature, SignatureExpired):
        return None
    except Exception:  # noqa: BLE001
        return None

    if not isinstance(data, dict):
        return None
    uid = data.get("uid")
    if not isinstance(uid, int):
        return None
    return data


def session_issued_at(token: str) -> int | None:
    """取会话的签发时间（Unix 秒）。解析失败返回 None。"""
    data = read_session_data(token)
    if data is None:
        return None
    iat = data.get("iat")
    return iat if isinstance(iat, int) else None


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