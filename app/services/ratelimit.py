"""Ограничение частоты в Redis: фиксированное окно (INCR + EXPIRE)."""
from redis.asyncio import Redis


async def hit(redis: Redis, key: str, limit: int, window_seconds: int) -> bool:
    """Засчитывает попытку. True — в пределах лимита, False — лимит превышен."""
    full_key = f"rl:{key}"
    count = await redis.incr(full_key)
    if count == 1:
        await redis.expire(full_key, window_seconds)
    return count <= limit


async def retry_after(redis: Redis, key: str) -> int:
    ttl = await redis.ttl(f"rl:{key}")
    return max(ttl, 0)
