import os
import json
import pytest
from pathlib import Path
from config.settings import (
    ConfigError,
    _required,
    _optional,
    get_spreadsheet_id,
    get_service_account_info,
    get_telegram_bot_token,
    get_tiger_credentials,
    get_moomoo_settings
)

@pytest.fixture
def clean_env(monkeypatch):
    # clear env
    monkeypatch.delenv("PORTFOLIO_SPREADSHEET_ID", raising=False)
    monkeypatch.delenv("GOOGLE_SERVICE_ACCOUNT_JSON", raising=False)
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TIGER_ID", raising=False)
    monkeypatch.delenv("TIGER_ACCOUNT", raising=False)
    monkeypatch.delenv("TIGER_PRIVATE_KEY", raising=False)

def test_required_missing(clean_env):
    with pytest.raises(ConfigError) as exc:
        _required("MISSING_KEY")
    assert "MISSING_KEY" in str(exc.value)

def test_optional_missing(clean_env):
    assert _optional("MISSING_KEY", "default") == "default"

def test_get_spreadsheet_id(clean_env, monkeypatch):
    monkeypatch.setenv("PORTFOLIO_SPREADSHEET_ID", "test_id")
    assert get_spreadsheet_id() == "test_id"

def test_get_service_account_info_json(clean_env, monkeypatch):
    data = {"project_id": "test"}
    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_JSON", json.dumps(data))
    
    res = get_service_account_info()
    assert res["project_id"] == "test"

def test_get_service_account_info_invalid_json(clean_env, monkeypatch):
    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_JSON", "invalid json")
    
    with pytest.raises(ConfigError) as exc:
        get_service_account_info()
    assert "valid JSON" in str(exc.value)

def test_get_service_account_info_file(clean_env, monkeypatch, tmp_path):
    data = {"project_id": "test2"}
    file_path = tmp_path / "creds.json"
    file_path.write_text(json.dumps(data))
    
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", str(file_path))
    
    res = get_service_account_info()
    assert res["project_id"] == "test2"

def test_get_service_account_info_missing_file(clean_env, monkeypatch):
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", "doesnt_exist.json")
    with pytest.raises(ConfigError):
        get_service_account_info()

def test_get_service_account_info_no_env(clean_env):
    with pytest.raises(ConfigError):
        get_service_account_info()

def test_get_telegram_bot_token(clean_env, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "token123")
    assert get_telegram_bot_token() == "token123"

def test_get_tiger_credentials(clean_env, monkeypatch):
    monkeypatch.setenv("TIGER_ID", "tid")
    monkeypatch.setenv("TIGER_ACCOUNT", "tacc")
    monkeypatch.setenv("TIGER_PRIVATE_KEY", "tpk")
    
    creds = get_tiger_credentials()
    assert creds["tiger_id"] == "tid"
    assert creds["account"] == "tacc"
    assert creds["private_key"] == "tpk"

def test_get_moomoo_settings(clean_env, monkeypatch):
    # Check defaults
    def_sets = get_moomoo_settings()
    assert def_sets["host"] == "127.0.0.1"
    assert def_sets["port"] == 11111
    assert def_sets["markets"] == ("US", "HK")
    
    # Check overrides
    monkeypatch.setenv("MOOMOO_PORT", "22222")
    monkeypatch.setenv("MOOMOO_MARKETS", "sg,us")
    
    over_sets = get_moomoo_settings()
    assert over_sets["port"] == 22222
    assert over_sets["markets"] == ("SG", "US")
