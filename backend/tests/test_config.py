import pytest
from pydantic import ValidationError

from app.config import ROOT_DIR, load_settings


def test_defaults_load_from_yaml(monkeypatch):
    for var in ("GROQ_API_KEY", "APP_ENV", "DATABASE_URL"):
        monkeypatch.delenv(var, raising=False)
    s = load_settings()
    assert s.llm.provider == "groq"
    assert s.citation_policy == "groww"
    assert s.api.max_input_chars == 500
    assert s.retrieval.final_top_k == 5
    assert s.threads.retention_days == 30


def test_env_overrides(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("DATABASE_URL", "sqlite:///other.db")
    s = load_settings()
    assert s.groq_api_key.get_secret_value() == "test-key"
    assert "test-key" not in repr(s) and "test-key" not in str(s.model_dump())
    assert s.app_env == "test"
    # relative SQLite paths are anchored at the project root, whatever the cwd
    assert s.database_url == f"sqlite:///{(ROOT_DIR / 'other.db').as_posix()}"


def test_invalid_config_fails_clearly(tmp_path):
    bad = tmp_path / "settings.yaml"
    bad.write_text("api:\n  max_input_chars: not-a-number\n", encoding="utf-8")
    with pytest.raises(ValidationError):
        load_settings(bad)
