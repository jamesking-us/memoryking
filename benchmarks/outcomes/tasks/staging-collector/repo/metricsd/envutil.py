"""Reading settings from an environment mapping (os.environ or a dict)."""

TRUE = {"1", "true", "yes", "on"}


def flag_enabled(env, name):
    return str(env.get(name, "")).strip().lower() in TRUE


def require(env, name):
    try:
        return env[name]
    except KeyError:
        raise RuntimeError(f"environment variable {name} is not set") from None
