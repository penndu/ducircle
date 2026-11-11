"""摘要生成的状态跟踪。

和抓取一样的问题：批量摘要跑在后台，HTTP 请求早返回 200 了。
后台失败时异常被 except 吞掉，用户面对的就是「点了没反应」——
跟任务根本没跑完全没法区分。

抓取那边已经有 crawl_status，这边照着做一份。
记录维度：
- state: running / done / failed
- generated: 生成了几篇
- skipped: 跳过几篇
- error: 失败原因（尤其是「没配 API Key」这种用户自己能解决的）
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

from app.core.ratelimit import RedisManager
from redis.exceptions import RedisError

logger = logging.getLogger("ducircle.summarystatus")

STATUS_KEY = "summary:status:{user_id}"
# 状态保留 2 小时——摘要任务比抓取慢（要逐篇调大模型），
# 用户看完结果再决定是否重来，2 小时够用
STATUS_TTL = 7200


def _key(user_id: int) -> str:
    return STATUS_KEY.format(user_id=user_id)


async def set_status(user_id: int, data: dict[str, Any]) -> None:
    """更新摘要状态。"""
    payload = dict(data)
    payload["updated_at"] = datetime.now().isoformat()
    try:
        await RedisManager.instance().client.set(
            _key(user_id), json.dumps(payload, ensure_ascii=False), ex=STATUS_TTL
        )
    except RedisError as exc:
        logger.warning("摘要状态写入失败: %s", exc)


async def get_status(user_id: int) -> dict[str, Any] | None:
    """读取摘要状态。没有记录时返回 None。"""
    try:
        raw = await RedisManager.instance().client.get(_key(user_id))
        if raw is None:
            return None
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        return json.loads(raw)
    except (RedisError, ValueError) as exc:
        logger.warning("摘要状态读取失败: %s", exc)
        return None
