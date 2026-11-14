"""Per-client fixed-window request throttling, shared by all API processes through Redis.

Runs before authentication, so failed bearer-token guesses count against the limit too.
If Redis is unreachable the limiter fails open (requests pass, a warning is logged):
/health/ready already reports that outage and job dispatch depends on Redis anyway.
"""
import logging
import time

from fastapi import Depends, HTTPException, Request
from redis import Redis
from redis.exceptions import RedisError

from app.settings import Settings, get_settings

log = logging.getLogger(__name__)
WINDOW_SECONDS = 60
_connections: dict[str, Redis] = {}


def _redis(url: str) -> Redis:
    if url not in _connections:
        _connections[url] = Redis.from_url(url, socket_connect_timeout=1, socket_timeout=1)
    return _connections[url]


def hit(settings: Settings, key: str, ttl: int) -> int:
    """Increment the counter for key and return the new count."""
    pipe = _redis(settings.redis_url).pipeline()
    pipe.incr(key)
    pipe.expire(key, ttl, nx=True)
    count, _ = pipe.execute()
    return int(count)


def client_id(request: Request) -> str:
    # Behind a reverse proxy this is the proxy's address unless uvicorn trusts its
    # X-Forwarded-For header (--forwarded-allow-ips), in which case it is the real client.
    return request.client.host if request.client else "unknown"


def rate_limit(request: Request, settings: Settings = Depends(get_settings)) -> None:
    limit = settings.rate_limit_per_minute
    if limit <= 0 or not request.url.path.startswith("/v1/"):
        return
    now = time.time()
    window = int(now // WINDOW_SECONDS)
    key = f"ratelimit:{client_id(request)}:{window}"
    try:
        count = hit(settings, key, WINDOW_SECONDS + 5)
    except RedisError:
        log.warning("Rate limiter unavailable (Redis error); allowing request")
        return
    if count > limit:
        retry_after = max(1, WINDOW_SECONDS - int(now) % WINDOW_SECONDS)
        raise HTTPException(status_code=429, detail="rate limit exceeded; retry later",
                            headers={"Retry-After": str(retry_after)})
