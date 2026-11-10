from functools import lru_cache

from redis import Redis
from supabase import Client, create_client

from app.config import settings


@lru_cache
def get_supabase() -> Client:
    return create_client(settings.supabase_url, settings.supabase_secret_key)


@lru_cache
def get_redis() -> Redis:
    return Redis.from_url(settings.redis_url, socket_connect_timeout=3)