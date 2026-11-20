"""Credentials: environment variables override the config file; nothing is required for offline commands."""

import json

import pytest

from meshlytics import config


def write(path, **values):
    path.write_text(json.dumps(values), encoding="utf-8")
    return path


def test_no_configuration(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    values = config.load()
    assert values["email"] is None and values["api_key"] is None
    assert values["openai_model"] == config.DEFAULT_OPENAI_MODEL
    assert config.config_file is None


def test_config_json_in_current_folder(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    write(tmp_path / "config.json", email="me@example.org", api_key="k1", openai_model="m1")
    config.load()
    assert (config.email, config.api_key, config.openai_model) == ("me@example.org", "k1", "m1")


def test_environment_overrides_file(tmp_path, monkeypatch):
    cfg = write(tmp_path / "settings.json", email="file@example.org", api_key="file-key")
    monkeypatch.setenv("NCBI_EMAIL", "env@example.org")
    monkeypatch.setenv("OPENALEX_API_KEY", "env-openalex")
    config.load(str(cfg))
    assert config.email == "env@example.org"
    assert config.api_key == "file-key"
    assert config.openalex_api_key == "env-openalex"


def test_meshlytics_config_variable(tmp_path, monkeypatch):
    cfg = write(tmp_path / "elsewhere.json", email="var@example.org")
    monkeypatch.setenv("MESHLYTICS_CONFIG", str(cfg))
    monkeypatch.chdir(tmp_path)
    config.load()
    assert config.email == "var@example.org"


def test_example_placeholder_email_is_ignored(tmp_path):
    cfg = write(tmp_path / "c.json", email="user@example.com")
    assert config.load(str(cfg))["email"] is None


def test_missing_config_files_are_reported(tmp_path, monkeypatch):
    with pytest.raises(FileNotFoundError):
        config.load(str(tmp_path / "missing.json"))
    monkeypatch.setenv("MESHLYTICS_CONFIG", str(tmp_path / "missing.json"))
    with pytest.raises(FileNotFoundError):
        config.load()
