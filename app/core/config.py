"""Environment-backed configuration, injected into create_app() instead of read ad hoc."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore")

    database_url: str
    jwt_secret_key: str
    jwt_access_token_minutes: int = 15
    jwt_refresh_token_days: int = 7
    mail_smtp_host: str = "mailpit"
    mail_smtp_port: int = 1025
    mail_from_address: str = "no-reply@pu.ufcg.edu.br"
    dashboard_base_url: str = "http://localhost:5173"
    qr_signing_private_key_hex: str
    osrm_base_url: str = "http://osrm:5000"
    minio_endpoint: str = "minio:9000"
    minio_access_key: str = "route_monitor"
    minio_secret_key: str = "change_me_in_local_env"
    minio_bucket_evidence: str = "evidence"
    minio_secure: bool = False


@lru_cache
def get_settings() -> Settings:
    # Required fields (database_url, jwt_secret_key, qr_signing_private_key_hex) come from
    # the environment; pydantic-settings fails loudly if they're missing, on purpose.
    return Settings()
