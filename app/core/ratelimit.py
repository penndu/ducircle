"""Redis 令牌桶限流、计数信号量、分布式锁。

用 Redis 而非进程内对象，是因为限流计数和调度锁必须跨进程共享——
FastAPI 多 worker 加调度器进程下，进程内计数会各算各的，等于没限。

所有 Redis 操作在 Redis 不可用时都降级放行：限流组件不该成为
整个服务的单点故障。代价是 Redis 挂掉时限流失效，这个取舍要明确。
"""

from __future__ import annotations

import logging
import secrets
import time

import redis.asyncio as aioredis
from redis.exceptions import RedisError

from app.core.config import get_settings

logger = logging.getLogger("ducircle.ratelimit")

# 令牌桶：惰性补充，无需后台定时器
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

redis.call('HSET', key, 'tokens', tokens, 'ts', now)
redis.call('EXPIRE', key, math.ceil(capacity / rate) + 60)

if tokens < cost then
  return 0
end

-- 回填扣除后的余量
redis.call('HSET', key, 'tokens', tokens - cost)
return 1
"""

# 比对持有者再删除，避免释放别人的锁
LUA_UNLOCK = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('DEL', KEYS[1])
end
return 0
"""


class RedisManager:
    """Redis 连接单例。

    整个进程共用一个连接池——每次调用新建连接会耗尽文件描述符，
    这是异步 Redis 最常见的生产事故。
    """

    _instance: RedisManager | None = None

    def __init__(self, url: str | None = None) -> None:
        self.url = url or get_settings().redis_url
        self._redis: aioredis.Redis | None = None

    @classmethod
    def instance(cls) -> "RedisManager":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @property
    def client(self) -> aioredis.Redis:
        if self._redis is None:
            self._redis = aioredis.from_url(
                self.url,
                encoding="utf-8",
                decode_responses=False,
                max_connections=20,
            )
        return self._redis

    async def close(self) -> None:
        if self._redis is not None:
            await self._redis.aclose()
            self._redis = None

    async def ping(self) -> bool:
        try:
            await self.client.ping()
            return True
        except RedisError as exc:
            logger.warning("Redis 不可用: %s", exc)
            return False


class RateLimiter:
    """双层限流：每用户 + 全局。任一层超限即拒绝。"""

    def __init__(self, redis: aioredis.Redis | None = None) -> None:
        self._redis = redis

    @property
    def redis(self) -> aioredis.Redis:
        return self._redis or RedisManager.instance().client

    async def allow(
        self,
        user_id: int | str | None,
        cost: float = 1.0,
        rate: float | None = None,
        burst: int | None = None,
    ) -> bool:
        """检查是否放行。

        rate/burst 不传则用配置默认值；中间件按路径类型传更严格的参数。
        """
        settings = get_settings()
        rate = rate if rate is not None else settings.rate_limit_qps
        burst = burst if burst is not None else settings.rate_limit_burst

        try:
            pipe = self.redis.pipeline(transaction=True)
            now = time.time()
            if user_id is not None:
                pipe.eval(
                    LUA_TAKE, 2, f"rl:u:{user_id}", "", now, cost, rate, burst
                )
            # 全层按用户层的 20 倍放行，避免用户少时全局桶形同虚设
            pipe.eval(
                LUA_TAKE, 2, "rl:global", "", now, cost, rate * 20, burst * 20
            )
            results = await pipe.execute()
            return all(results)
        except RedisError as exc:
            logger.warning("限流检查失败，放行: %s", exc)
            return True


class Semaphore:
    """Redis 计数信号量，限制全局同时执行的抓取任务数。

    用计数而非分布式锁，是因为抓取任务允许多个同时跑，
    只是不能超过并发上限。TTL 兜住进程崩溃后的计数泄漏。
    """

    def __init__(self, name: str, limit: int, ttl: int = 600) -> None:
        self.key = f"sem:{name}"
        self.limit = limit
        self.ttl = ttl

    async def acquire(self, redis: aioredis.Redis) -> bool:
        try:
            pipe = redis.pipeline(transaction=True)
            pipe.incr(self.key)
            pipe.expire(self.key, self.ttl)
            count, _ = await pipe.execute()
            if count > self.limit:
                await redis.decr(self.key)
                return False
            return True
        except RedisError:
            return True  # Redis 故障时放行

    async def release(self, redis: aioredis.Redis) -> None:
        try:
            current = await redis.get(self.key)
            # 计数已被 TTL 清掉时不要减成负数
            if current is not None and int(current) > 0:
                await redis.decr(self.key)
        except (RedisError, ValueError, TypeError):
            pass


class DistributedLock:
    """SET NX 分布式锁，保证定时任务全局单例执行。

    用法::

        async with DistributedLock("crawl", ttl=300) as redis:
            if redis is None:
                return  # 没抢到锁，本次跳过

    抢锁失败时 __aenter__ 返回 None 而不是抛异常——
    定时任务「没抢到锁」是正常情况，不是错误。
    """

    def __init__(self, name: str, ttl: int = 3600) -> None:
        self.key = f"lock:{name}"
        self.ttl = ttl
        self._token: str | None = None

    async def __aenter__(self) -> aioredis.Redis | None:
        self._token = secrets.token_hex(16)
        redis = RedisManager.instance().client
        try:
            acquired = await redis.set(
                self.key, self._token, nx=True, ex=self.ttl
            )
            return redis if acquired else None
        except RedisError as exc:
            logger.warning("分布式锁不可用，放行: %s", exc)
            return redis

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self._token is None:
            return
        try:
            redis = RedisManager.instance().client
            await redis.eval(LUA_UNLOCK, 1, self.key, self._token)
        except RedisError:
            pass
        finally:
            self._token = None