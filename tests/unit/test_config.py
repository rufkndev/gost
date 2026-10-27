from typing import Any

import pytest
from pydantic import ValidationError

from app.config import Settings


def make(**env: Any) -> Settings:
    return Settings(_env_file=None, **env)


def test_defaults() -> None:
    s = make()
    assert s.price_rub == 300
    assert s.llm_model == "gpt-6-luna" and s.llm_escalation_model == "gpt-6-sol"


def test_csv_lists_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ADMIN_IDS", "1, 22 ,333")
    monkeypatch.setenv("BETA_USER_IDS", "")
    s = make()
    assert s.admin_ids == [1, 22, 333]
    assert s.beta_user_ids == []


def test_debug_text_logs_forbidden_in_prod() -> None:
    with pytest.raises(ValidationError, match="DEBUG_TEXT_LOGS"):
        make(environment="prod", debug_text_logs=True)
    assert make(environment="dev", debug_text_logs=True).debug_text_logs


def test_thresholds_order() -> None:
    with pytest.raises(ValidationError):
        make(classify_accept_threshold=0.5, classify_llm_threshold=0.6)


def test_llm_requires_key() -> None:
    with pytest.raises(ValidationError, match="OPENAI_API_KEY"):
        make(llm_enabled=True)
    assert make(llm_enabled=True, openai_api_key="sk-test").llm_enabled


def test_encryption_requires_key() -> None:
    with pytest.raises(ValidationError, match="STORAGE_FERNET_KEY"):
        make(storage_encryption=True)
