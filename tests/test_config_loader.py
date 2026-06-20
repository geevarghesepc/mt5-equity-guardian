import os
import sys
import tempfile

import pytest
import yaml

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))

from config_loader import load_config, replace_config


def test_load_config_returns_empty_dict_for_missing_file():
    with tempfile.TemporaryDirectory() as tmp:
        original = os.getcwd()
        try:
            os.chdir(tmp)
            assert load_config() == {}
        finally:
            os.chdir(original)


def test_load_config_returns_empty_dict_for_empty_yaml():
    with tempfile.TemporaryDirectory() as tmp:
        original = os.getcwd()
        try:
            os.chdir(tmp)
            with open("config.yaml", "w", encoding="utf-8") as f:
                f.write("")
            assert load_config() == {}
        finally:
            os.chdir(original)


def test_replace_config_drops_removed_keys():
    target = {"mode": "live", "telegram": {"token": "abc", "chat_id": "1"}}
    source = {"mode": "observe"}
    replace_config(target, source)
    assert target == {"mode": "observe"}
    assert "telegram" not in target


def test_load_config_reads_valid_yaml():
    with tempfile.TemporaryDirectory() as tmp:
        original = os.getcwd()
        try:
            os.chdir(tmp)
            data = {"mode": "observe", "loop": {"interval_ms": 500}}
            with open("config.yaml", "w", encoding="utf-8") as f:
                yaml.safe_dump(data, f)
            assert load_config() == data
        finally:
            os.chdir(original)


def test_load_config_raises_on_invalid_yaml():
    with tempfile.TemporaryDirectory() as tmp:
        original = os.getcwd()
        try:
            os.chdir(tmp)
            with open("config.yaml", "w", encoding="utf-8") as f:
                f.write("mode: [unclosed")
            with pytest.raises(yaml.YAMLError):
                load_config()
        finally:
            os.chdir(original)


def test_load_config_applies_env_overrides(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        original = os.getcwd()
        try:
            os.chdir(tmp)
            data = {
                "account": {"login": 1, "password": "old", "server": "old-server"},
                "telegram": {"token": "", "chat_id": ""},
            }
            with open("config.yaml", "w", encoding="utf-8") as f:
                yaml.safe_dump(data, f)

            monkeypatch.setenv("MT5_LOGIN", "999")
            monkeypatch.setenv("MT5_PASSWORD", "secret")
            monkeypatch.setenv("TELEGRAM_TOKEN", "tok")

            loaded = load_config()
            assert loaded["account"]["login"] == 999
            assert loaded["account"]["password"] == "secret"
            assert loaded["telegram"]["token"] == "tok"
        finally:
            os.chdir(original)
