"""Client, get_redis dependency, check() for health."""

from redis.asyncio import Redis

from src.config import RedisSettings

_client: Redis | None = None


def init_redis(settings: RedisSettings) -> None:
    global _client
    _client = Redis.from_url(
        settings.url,
        socket_timeout=settings.socket_timeout_seconds,
        socket_connect_timeout=settings.socket_timeout_seconds,
        decode_responses=True,
    )


async def close_redis() -> None:
    if _client is not None:
        await _client.aclose()


def get_redis() -> Redis:
    if _client is None:
        raise RuntimeError("init_redis() must run before get_redis() is used")
    return _client


async def check() -> None:
    await get_redis().ping()
