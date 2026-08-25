"""Configuration loaded from the environment.

Every setting a service needs comes from environment variables -- no secrets are
read from files inside the image, so the same image runs unchanged in Compose
and in Kubernetes (where the values arrive from a ConfigMap or Secret).
"""

from __future__ import annotations

from functools import cache

from pydantic import Field, computed_field
from pydantic_settings import BaseSettings, SettingsConfigDict


class ServiceSettings(BaseSettings):
    """Base settings shared by all services.

    Services subclass this and set ``service_name`` / ``db_name`` defaults.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    service_name: str = "retailpulse-service"
    environment: str = "local"
    log_level: str = "INFO"

    # ---------- Postgres ----------
    postgres_user: str = "retailpulse"
    # Local-development default only. Every deployed environment overrides this
    # from a Kubernetes Secret; nothing here is a real credential.
    postgres_password: str = "retailpulse_dev_password"  # noqa: S105
    postgres_host: str = "localhost"
    postgres_port: int = 5432
    db_name: str = "retailpulse"

    # Set directly to override the assembled URL (used by tests to point at SQLite).
    database_url_override: str | None = None

    # Connection pool. Sized small on purpose: with N service replicas each
    # holding a pool, Postgres' max_connections is the real constraint.
    db_pool_size: int = 5
    db_max_overflow: int = 10
    db_pool_pre_ping: bool = True

    # ---------- Redis ----------
    redis_url: str = "redis://localhost:6379/0"
    cache_ttl_seconds: int = 300
    rate_limit_requests_per_minute: int = 100

    # ---------- Kafka ----------
    kafka_bootstrap_servers: str = "localhost:9092"
    kafka_consumer_group: str = "retailpulse"
    kafka_max_retries: int = 3
    kafka_retry_backoff_seconds: float = 0.5

    # ---------- Auth ----------
    # Deliberately an obvious placeholder: if this value ever reaches a
    # deployed environment it should be glaringly wrong in the logs.
    jwt_secret_key: str = "change-me-in-production"  # noqa: S105
    jwt_algorithm: str = "HS256"
    jwt_access_token_expire_minutes: int = 60

    # ---------- HTTP ----------
    cors_allow_origins: list[str] = Field(default_factory=lambda: ["http://localhost:5173"])
    http_client_timeout_seconds: float = 5.0

    @computed_field  # type: ignore[prop-decorator]
    @property
    def database_url(self) -> str:
        """SQLAlchemy URL for this service's own database."""
        if self.database_url_override:
            return self.database_url_override
        return (
            f"postgresql+psycopg://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.db_name}"
        )

    @property
    def is_local(self) -> bool:
        return self.environment == "local"


@cache
def get_settings(cls: type[ServiceSettings] = ServiceSettings) -> ServiceSettings:
    """Cached settings instance -- env is read once per process."""
    return cls()
