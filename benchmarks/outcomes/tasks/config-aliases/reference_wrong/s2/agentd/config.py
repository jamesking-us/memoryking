"""Configuration for agentd, read from a JSON file."""

import json


class ConfigError(Exception):
    pass


DEFAULTS = {"timeout_seconds": 30, "max_retries": 3, "log_level": "info", "workers": 2}
LOG_LEVELS = ("debug", "info", "warning", "error")


class Settings:
    def __init__(self, values):
        self.timeout_seconds = values["timeout_seconds"]
        self.max_retries = values["max_retries"]
        self.log_level = values["log_level"]
        self.workers = values["workers"]

    def as_dict(self):
        return dict(vars(self))


def validate(values):
    if not isinstance(values["timeout_seconds"], (int, float)) or values["timeout_seconds"] <= 0:
        raise ConfigError("timeout_seconds must be a positive number")
    if not isinstance(values["max_retries"], int) or values["max_retries"] < 0:
        raise ConfigError("max_retries must be a non-negative integer")
    if not isinstance(values["workers"], int) or values["workers"] < 1:
        raise ConfigError("workers must be a positive integer")
    if values["log_level"] not in LOG_LEVELS:
        raise ConfigError("log_level must be one of " + ", ".join(LOG_LEVELS))
    return values


def parse(raw):
    unknown = sorted(set(raw) - set(DEFAULTS))
    if unknown:
        raise ConfigError("unknown key: " + ", ".join(unknown))
    return Settings(validate({**DEFAULTS, **raw}))


def load(path):
    try:
        with open(path) as handle:
            raw = json.load(handle)
    except (OSError, ValueError) as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError("config must be a JSON object")
    return parse(raw)
