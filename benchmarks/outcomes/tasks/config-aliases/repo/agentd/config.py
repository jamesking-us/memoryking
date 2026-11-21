"""Configuration for agentd, read from a JSON file."""

import json


class ConfigError(Exception):
    pass


DEFAULTS = {"timeout": 30, "retries": 3, "log_level": "info", "workers": 2}


class Settings:
    def __init__(self, values):
        self.timeout = values["timeout"]
        self.retries = values["retries"]
        self.log_level = values["log_level"]
        self.workers = values["workers"]

    def as_dict(self):
        return dict(vars(self))


def validate(values):
    if not isinstance(values["timeout"], (int, float)) or values["timeout"] <= 0:
        raise ConfigError("timeout must be a positive number")
    if not isinstance(values["retries"], int) or values["retries"] < 0:
        raise ConfigError("retries must be a non-negative integer")
    if not isinstance(values["workers"], int) or values["workers"] < 1:
        raise ConfigError("workers must be a positive integer")
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
