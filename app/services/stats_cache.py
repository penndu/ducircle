"""统计快照缓存。

后台看板读快照而不是实时 count——数据量大时实时统计很慢，
而看板不需要秒级实时。快照由定时任务刷新。
"""

from __future__ import annotations

import json
import logging

from app.core.ratelimit import RedisManager
from redis.exceptions import RedisError

logger = logging.getLogger("fcircle.stats")

SNAPSHOT_KEY = "stats:snapshot"
# 快照比调度周期长一点过期，避免任务没跑就彻底没数据
SNAPSHOT_TTL = 86400


async def save_snapshot(data: dict) -> None:
    try:
        await RedisManager.instance().client.set(
            SNAPSHOT_KEY, json.dumps(data, ensure_ascii=False), ex=SNAPSHOT_TTL
        )
    except RedisError as exc:
        logger.warning("统计快照写入失败: %s", exc)


async def load_snapshot() -> dict | None:
    try:
        raw = await RedisManager.instance().client.get(SNAPSHOT_KEY)
        if raw is None:
            return None
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        return json.loads(raw)
    except (RedisError, ValueError) as exc:
        logger.warning("统计快照读取失败: %s", exc)
        return None


async def invalidate_snapshot() -> None:
    try:
        await RedisManager.instance().client.delete(SNAPSHOT_KEY)
    except RedisError:
        pass