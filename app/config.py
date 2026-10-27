"""Конфигурация приложения. Все значения — из переменных окружения или `.env`."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

ROOT_DIR = Path(__file__).resolve().parent.parent


def _split_csv(value: Any) -> Any:
    """`1,2, 3` → ["1", "2", "3"]; списки и пустые значения пропускаются как есть."""
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    return value


IntList = Annotated[list[int], NoDecode]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    environment: Literal["dev", "test", "prod"] = "dev"
    log_level: str = "INFO"
    # Разрешает текст документов в логах. Только для отладки на корпусе, в prod запрещено.
    debug_text_logs: bool = False
    sentry_dsn: str = ""

    # Telegram
    bot_token: SecretStr = SecretStr("")
    bot_username: str = ""
    admin_ids: IntList = Field(default_factory=list)

    # Инфраструктура
    database_url: str = "postgresql+asyncpg://gost:gost@localhost:5432/gost"
    redis_url: str = "redis://localhost:6379/0"

    # ЮKassa
    yookassa_shop_id: str = ""
    yookassa_secret_key: SecretStr = SecretStr("")
    price_rub: int = Field(default=300, gt=0)
    payment_ttl_minutes: int = Field(default=30, gt=0)
    receipts_enabled: bool = False
    receipt_vat_code: int = Field(default=1, ge=1, le=12)

    # ИИ
    llm_enabled: bool = False
    openai_api_key: SecretStr = SecretStr("")
    llm_model: str = "gpt-6-luna"
    llm_escalation_model: str = "gpt-6-sol"
    llm_max_cost_per_doc_usd: float = Field(default=0.05, ge=0)
    llm_max_blocks: int = Field(default=60, gt=0)
    llm_timeout_s: int = Field(default=60, gt=0)

    # Классификация и вопросы пользователю
    classify_accept_threshold: float = Field(default=0.90, ge=0, le=1)
    classify_llm_threshold: float = Field(default=0.60, ge=0, le=1)
    max_user_questions: int = Field(default=5, ge=0)
    question_timeout_minutes: int = Field(default=30, gt=0)

    # Бета
    beta_mode: bool = False
    beta_user_ids: IntList = Field(default_factory=list)

    # Файлы и лимиты
    storage_dir: Path = Path("/data/files")
    storage_encryption: bool = False
    storage_fernet_key: SecretStr = SecretStr("")
    max_upload_mb: int = Field(default=20, gt=0)
    max_unzipped_mb: int = Field(default=200, gt=0)
    process_timeout_s: int = Field(default=120, gt=0)
    rate_limit_docs_per_hour: int = Field(default=10, gt=0)
    rate_limit_msgs_per_minute: int = Field(default=30, gt=0)

    # Сроки хранения
    retention_unpaid_hours: int = Field(default=2, gt=0)
    retention_paid_hours: int = Field(default=24, gt=0)
    retention_complaint_days: int = Field(default=14, gt=0)

    # Профили
    profiles_dir: Path = ROOT_DIR / "app" / "profiles"

    # Реквизиты для юридических документов
    ip_name: str = ""
    inn: str = ""
    ogrnip: str = ""
    ip_registration: str = ""  # кем и когда зарегистрирован ИП (ст. 9 ЗоЗПП)
    contact_email: str = ""
    support_contact: str = ""
    terms_version: str = "1"

    @field_validator("admin_ids", "beta_user_ids", mode="before")
    @classmethod
    def _parse_csv(cls, value: Any) -> Any:
        return _split_csv(value)

    @model_validator(mode="after")
    def _check_consistency(self) -> Settings:
        if self.classify_llm_threshold > self.classify_accept_threshold:
            raise ValueError("CLASSIFY_LLM_THRESHOLD must not exceed CLASSIFY_ACCEPT_THRESHOLD")
        if self.environment == "prod" and self.debug_text_logs:
            raise ValueError("DEBUG_TEXT_LOGS is forbidden in production")
        if self.storage_encryption and not self.storage_fernet_key.get_secret_value():
            raise ValueError("STORAGE_ENCRYPTION requires STORAGE_FERNET_KEY")
        if self.llm_enabled and not self.openai_api_key.get_secret_value():
            raise ValueError("LLM_ENABLED requires OPENAI_API_KEY")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
