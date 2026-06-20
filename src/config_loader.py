import os
import logging

import yaml

logger = logging.getLogger(__name__)

ENV_OVERRIDES = {
    "MT5_LOGIN": ("account", "login", int),
    "MT5_PASSWORD": ("account", "password", str),
    "MT5_SERVER": ("account", "server", str),
    "TELEGRAM_TOKEN": ("telegram", "token", str),
    "TELEGRAM_CHAT_ID": ("telegram", "chat_id", str),
}


def _apply_env_overrides(config):
    """Overlay secrets from environment variables when set."""
    for env_key, (section, field, caster) in ENV_OVERRIDES.items():
        value = os.environ.get(env_key)
        if value is None or value == "":
            continue
        config.setdefault(section, {})
        try:
            config[section][field] = caster(value)
        except (TypeError, ValueError):
            logger.warning("Invalid value for %s; ignoring env override.", env_key)


def load_config(path="config.yaml"):
    """Load config.yaml from the project root. Returns {} if missing or empty."""
    if not os.path.exists(path):
        if os.path.exists("config.example.yaml"):
            logger.warning("Config not found at %s, falling back to config.example.yaml.", path)
            path = "config.example.yaml"
        else:
            logger.warning("Config not found at %s, using defaults.", path)
            return {}

    try:
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except (OSError, yaml.YAMLError) as exc:
        logger.error("Failed to load config from %s: %s", path, exc)
        raise

    if not data:
        logger.warning("Config at %s is empty, using defaults.", path)
        return {}
    if not isinstance(data, dict):
        logger.warning("Config at %s is not a mapping, using defaults.", path)
        return {}

    _apply_env_overrides(data)
    return data


def replace_config(target, source):
    """Replace in-memory config so removed YAML keys do not linger after hot-reload."""
    target.clear()
    target.update(source)
