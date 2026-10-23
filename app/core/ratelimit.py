"""Redis 令牌桶限流与分布式锁。

用 Redis 而非进程内对象，是因为限流计数和调度锁必须跨进程共享——
FastAPI 多 worker 加调度器进程下，进程内计数会各算各的，等于没限。
"""

from __future__ import annotations

import time

import redis.asyncio as aioredis
from redis.exceptions import RedisError

from app.core.config import get_settings


class RateLimiter:
    """双层限流：每用户 + 全局。任一层超限即拒绝。"""

    def __init__(self, redis: aioredis.Redis | None = None) -> None:
        self._redis = redis
        self._settings = get_settings()

    @property
    def redis(self) -> aioredis.Redis:
        if self._redis is None:
            self._redis = aioredis.from_url(self._settings.redis_url)
        return self._redis

    async def allow(self, user_id: int | None, cost: float = 1.0) -> bool:
        settings = self._settings
        try:
            pipe = self.redis.pipeline(transaction=True)
            now = time.time()
            if user_id is not None:
                pipe.eval(LUA_TAKE, 2, f"rl:u:{user_id}", "", now, cost,
                          settings.rate_limit_qps, settings.rate_limit_burst)
            pipe.eval(LUA_TAKE, 2, "rl:global", "", now, cost,
                      settings.rate_limit_qps * 20, settings.rate_limit_burst * 20)
            results = await pipe.execute()
            return all(results)
        except RedisError:
            # Redis 故障时放行，避免限流组件成为单点故障。
            # 若要严格限流，把这里改成抛出并让中间件返回 503。
            return True


# 令牌桶：惰性补充，无需定时器
LUA_TAKE = """
local key = KEYS[1]
local now = tonumber(ARGV[1])
local cost = tonumber(ARGV[2])
local rate = tonumber(ARGV[3])
local capacity = tonumber(ARGV[4])

local bucket = redis.call('HMGET', key, 'tokens', 'ts')
local tokens = tonumber(bucket[1])
local ts = tonumber(bucket[2])

if tokens == nil then
  tokens = capacity
  ts = now
end

local delta = math.max(0, now - ts)
tokens = math.min(capacity, tokens + delta * rate)

if tokens < cost then
  redis.call('HSET', key, 'tokens', tokens, 'ts', now)
  redis.call('EXPIRE', key, math.ceil(capacity / rate) + 60)
  return 0
end

tokens = tokens - cost
redis.call('HSET', key, 'tokens', tokens, 'ts', now)
redis.call('EXPIRE', key, math.ceil(capacity / rate) + 60)
return 1
"""


class Semaphore:
    """Redis 计数信号量，限制全局同时执行的抓取任务数。"""

    def __init__(self, name: str, limit: int, ttl: int = 300) -> None:
        self.key = f"sem:{name}"
        self.limit = limit
        self.ttl = ttl

    async def acquire(self, redis: aioredis.Redis) -> bool:
        pipe = redis.pipeline(transaction=True)
        pipe.incr(self.key)
        pipe.expire(self.key, self.ttl)
        count, _ = await pipe.execute()
        return count <= self.limit

    async def release(self, redis: aioredis.Redis) -> None:
        try:
            await redis.decr(self.key)
        except RedisError:
            pass


class DistributedLock:
    """SET NX 分布式锁，保证定时任务全局单例执行。

    多 worker 下若不加锁，同一个时间点会重复抓取所有站点。
    """

    def __init__(self, name: str, ttl: int = 3600) -> None:
        self.key = f"lock:{name}"
        self.ttl = ttl
        self._token = None

    async def __aenter__(self, redis: aioredis.Redis) -> bool:
        self._token = secrets_token()
        try:
            return await redis.set(self.key, self._token, nx=True, ex=self.ttl)
        except RedisError:
            return True  # Redis 故障时不阻塞任务

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self._token is None:
            return
        try:
            await redis_delete_if_owner(redis_module(), self.key, self._token)
        except RedisError:
            pass


def secrets_token() -> str:
    import secrets

    return secrets.token_hex(16)


def redis_module():
    import redis.asyncio as aioredis

    from app.core.config import get_settings

    return aioredis.from_url(get_settings().redis_url)


async def redis_delete_if_owner(redis, key: str, token: str) -> None:
    """比对持有者再删除，避免释放别人的锁。"""
    lua = """
    if redis.call('GET', KEYS[1]) == ARGV[1] then
      return redis.call('DEL', KEYS[1])
    end
    return 0
    """
    await redis.eval(lua, 1, key, token)