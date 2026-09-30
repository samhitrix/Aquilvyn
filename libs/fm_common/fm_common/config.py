"""Settings shared by every FolioSense service. Each service subclasses ``CommonSettings``
for its own knobs; everything is env-driven so an engine can be re-pointed without code."""
from __future__ import annotations

from functools import lru_cache
from typing import Literal
from urllib.parse import quote

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class CommonSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    service_name: str = "foliosense"
    environment: Literal["local", "dev", "staging", "prod", "test"] = "local"
    api_prefix: str = "/api/v1"
    cors_origins: list[str] = ["http://localhost:3000", "http://localhost:8080"]

    # --- database: set the parts once in .env; the URL is built from them ---
    postgres_user: str = "foliosense"
    postgres_password: str = "foliosense"
    postgres_db: str = "foliosense"
    db_host: str = "localhost"          # docker-compose sets pgbouncer (services) / postgres (migrations)
    db_port: int = 5432
    database_url: str = ""              # optional full override; normally leave unset
    db_pool_size: int = 5
    db_max_overflow: int = 5
    db_behind_pgbouncer: bool = False
    db_echo: bool = False

    redis_url: str = "redis://localhost:6379/0"

    # --- identity (shared so every service can verify tokens locally) ---
    jwt_secret: str = Field(default="dev-only-secret-change-me-0123456789abcdef", min_length=32)
    jwt_algorithm: str = "HS256"
    jwt_issuer: str = "foliosense"
    jwt_audience: str = "foliosense-api"
    access_token_ttl_seconds: int = 900

    # --- rate limiting ---
    rate_limit_enabled: bool = True
    rate_limit_default: int = 300
    rate_limit_window_seconds: int = 60
    rate_limit_auth: int = 10

    # --- cache ---
    cache_l1_max_items: int = 5_000
    cache_l1_ttl_seconds: float = 5
    cache_l2_ttl_seconds: int = 60

    # --- internal service discovery (docker-compose DNS names by default) ---
    identity_url: str = "http://localhost:8001"
    portfolio_url: str = "http://localhost:8002"
    market_url: str = "http://localhost:8003"
    analytics_url: str = "http://localhost:8004"
    advisor_url: str = "http://localhost:8005"
    readiness_url: str = "http://localhost:8007"
    dashboard_url: str = "http://localhost:8006"
    internal_timeout_seconds: float = 10.0

    # --- observability ---
    log_level: str = "INFO"
    otel_enabled: bool = False
    otel_exporter_otlp_endpoint: str = "http://localhost:4317"

    @model_validator(mode="after")
    def _assemble_database_url(self) -> CommonSettings:
        if not self.database_url:
            self.database_url = build_database_url(self.postgres_user, self.postgres_password, self.db_host, self.db_port, self.postgres_db)
        return self

    @property
    def is_test(self) -> bool:
        return self.environment == "test"


def build_database_url(user: str, password: str, host: str, port: int | str, db: str) -> str:
    """Password/user are percent-encoded, so any characters (@ : / # …) are safe."""
    return f"postgresql+asyncpg://{quote(user, safe='')}:{quote(password, safe='')}@{host}:{port}/{quote(db, safe='')}"


@lru_cache
def get_settings() -> CommonSettings:
    return CommonSettings()


settings = get_settings()
