"""密码哈希、API Key 生成与校验、URL 规范化。"""

from __future__ import annotations

import hashlib
import re
import secrets
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import bcrypt

# URL 中常见的追踪参数，抓取时剥离以提高去重准确率
TRACKING_PARAMS = frozenset(
    {
        "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
        "spm", "from", "ref", "referer", "referrer", "share_token",
        "_ga", "yclid", "fbclid", "gclid",
    }
)


def hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))
    except ValueError:
        return False


def generate_api_key() -> str:
    """生成用户 API Key，格式 fc_ + 32 位十六进制。"""
    return "fc_" + secrets.token_hex(16)


def hash_api_key(api_key: str) -> str:
    """API Key 的存储形式。数据库存哈希，比明文泄露风险低。"""
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()


def verify_api_key(api_key: str, stored_hash: str) -> bool:
    return secrets.compare_digest(hash_api_key(api_key), stored_hash)


def normalize_url(url: str, base: str | None = None) -> str:
    """规范化 URL 用于去重。

    统一小写协议与主机名、去掉 www. 前缀、剥离追踪参数、去掉 fragment、
    去掉末尾斜杠。这样同一篇文章带 utm 参数分享、或从 www 与裸域分别访问，
    都会被识别为同一篇。
    """
    if not url:
        return ""

    url = url.strip()
    if base:
        parts = urlsplit(url)
        if not parts.scheme:
            url = f"{base.rstrip('/')}/{url.lstrip('/')}"

    parts = urlsplit(url)
    if not parts.scheme or not parts.netloc:
        return url

    scheme = parts.scheme.lower()
    if scheme == "http":
        scheme = "https"

    netloc = parts.netloc.lower()
    if netloc.startswith("www."):
        netloc = netloc[4:]
    # 去掉端口，仅保留默认端口
    if netloc.endswith(":80") or netloc.endswith(":443"):
        netloc = netloc.rsplit(":", 1)[0]

    query_pairs = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if k.lower() not in TRACKING_PARAMS
    ]
    query = urlencode(sorted(query_pairs))

    path = parts.path.rstrip("/") or "/"

    return urlunsplit((scheme, netloc, path, query, ""))


def guid_hash(url: str) -> str:
    """文章去重指纹。"""
    return hashlib.sha1(normalize_url(url).encode("utf-8")).hexdigest()


def site_hash(url: str) -> str:
    """站点去重指纹，忽略路径只取主机部分。"""
    netloc = urlsplit(normalize_url(url)).netloc
    return hashlib.sha1(netloc.encode("utf-8")).hexdigest()


def url_hash(url: str) -> str:
    """完整 URL 的去重指纹，保留路径。

    site_hash 只取主机名，适合「同一个博客只能加一次」；
    但友链页是按完整地址区分的（同一个站点的 /friends/ 和 /links/
    是两张不同的清单页），所以需要保留路径的版本。
    """
    return hashlib.sha1(normalize_url(url).encode("utf-8")).hexdigest()


def valid_username(name: str) -> bool:
    return bool(re.fullmatch(r"[a-zA-Z0-9_.-]{3,32}", name))