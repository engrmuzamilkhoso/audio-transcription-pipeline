import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    supabase_url: str
    supabase_secret_key: str
    supabase_bucket: str
    redis_url: str
    max_file_mb: int
    max_duration_s: float
    whisper_model: str


def load_settings() -> Settings:
    return Settings(
        supabase_url=os.environ["SUPABASE_URL"],
        supabase_secret_key=os.environ["SUPABASE_SECRET_KEY"],
        supabase_bucket=os.getenv("SUPABASE_BUCKET", "audio"),
        redis_url=os.getenv("REDIS_URL", "redis://redis:6379/0"),
        max_file_mb=int(os.getenv("MAX_FILE_MB", "50")),
        max_duration_s=float(os.getenv("MAX_DURATION_S", "1800")),
        whisper_model=os.getenv("WHISPER_MODEL", "base"),
    )


settings = load_settings()