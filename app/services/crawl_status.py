"""手动抓取的状态跟踪。

用 Redis 记录最近一次手动触发的进度，让前端能轮询出
「抓到几篇、失败几个、还在跑吗」——而不是只弹一句「已触发」
然后让用户自己去猜。

**为什么必须这么做**：后台任务失败时异常会被 except 吞掉，
HTTP 请求早就返回 200 了。前端如果只知道「已触发」，
用户面对的就是「点了没反应」——和任务根本没跑完全没法区分。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

from app.core.ratelimit import RedisManager
from redis.exceptions import RedisError

logger = logging.getLogger("ducircle.crawlstatus")

STATUS_KEY = "crawl:status:{user_id}"
# 状态保留 1 小时——足够用户看完结果，之后自动过期
STATUS_TTL = 3600


def _key(user_id: int) -> str:
    return STATUS_KEY.format(user_id=user_id)


async def set_status(user_id: int, data: dict[str, Any]) -> None:
    """更新抓取状态。"""
    payload = dict(data)
    payload["updated_at"] = datetime.now().isoformat()
    try:
        await RedisManager.instance().client.set(
            _key(user_id), json.dumps(payload, ensure_ascii=False), ex=STATUS_TTL
        )
    except RedisError as exc:
        logger.warning("抓取状态写入失败: %s", exc)


async def get_status(user_id: int) -> dict[str, Any] | None:
    """读取抓取状态。没有记录时返回 None。"""
    try:
        raw = await RedisManager.instance().client.get(_key(user_id))
        if raw is None:
            return None
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        return json.loads(raw)
    except (RedisError, ValueError) as exc:
        logger.warning("抓取状态读取失败: %s", exc)
        return None
