"""Environment configuration helpers.

A tiny, dependency-free layer for reading configuration from the environment
(optionally seeded from a local ``.env`` file).  Keeping this logic in one place
means no settings module ever has to embed a secret and the same code powers
development, test and production.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, unquote, urlparse

from django.core.exceptions import ImproperlyConfigured

_TRUE_VALUES = {"1", "true", "yes", "on"}


def load_dotenv(path: Path | str) -> None:
    """Populate ``os.environ`` from a ``.env`` file without overriding values.

    The parser intentionally supports only the small subset used by this
    project: ``KEY=value`` lines, optional ``export`` prefix, ``#`` comments and
    single/double quoted values.  It never overwrites an already-set variable.
    """
    dotenv_path = Path(path)
    if not dotenv_path.is_file():
        return

    for raw_line in dotenv_path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        os.environ.setdefault(key, value)


def env(name: str, default: Any = None, *, required: bool = False) -> str | None:
    """Return an environment variable as a string."""
    value = os.environ.get(name, default)
    if required and (value is None or value == ""):
        raise ImproperlyConfigured(
            f"The required environment variable {name!r} is not set. "
            "See .env.example for the expected configuration."
        )
    return value


def env_bool(name: str, default: bool = False) -> bool:
    """Return an environment variable as a boolean."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in _TRUE_VALUES


def env_int(name: str, default: int = 0) -> int:
    """Return an environment variable as an integer."""
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ImproperlyConfigured(
            f"Environment variable {name!r} must be an integer, got {raw!r}."
        ) from exc


def env_list(name: str, default: list[str] | None = None, *, sep: str = ",") -> list[str]:
    """Return an environment variable as a list of trimmed strings."""
    raw = os.environ.get(name)
    if not raw:
        return list(default or [])
    return [item.strip() for item in raw.split(sep) if item.strip()]


def database_from_url(
    url: str | None,
    *,
    conn_max_age: int = 0,
    conn_health_checks: bool = True,
    ssl_require: bool = False,
) -> dict[str, Any]:
    """Translate a ``DATABASE_URL`` into a Django ``DATABASES['default']`` dict.

    PostgreSQL is the first-class citizen, but SQLite is tolerated for running
    the isolated test configuration.
    """
    if not url:
        raise ImproperlyConfigured(
            "DATABASE_URL is not configured. Set it in your environment or .env "
            "(see .env.example)."
        )

    parsed = urlparse(url)
    scheme = parsed.scheme.split("+")[0].lower()

    if scheme in {"postgres", "postgresql", "psql"}:
        query = dict(parse_qsl(parsed.query))
        options: dict[str, Any] = {}
        sslmode = query.pop("sslmode", None)
        if ssl_require and not sslmode:
            sslmode = "require"
        if sslmode:
            options["sslmode"] = sslmode

        config: dict[str, Any] = {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": unquote(parsed.path.lstrip("/")) or "postgres",
            "USER": unquote(parsed.username or ""),
            "PASSWORD": unquote(parsed.password or ""),
            "HOST": parsed.hostname or "",
            "PORT": str(parsed.port or ""),
            "CONN_MAX_AGE": conn_max_age,
            "CONN_HEALTH_CHECKS": conn_health_checks,
            "OPTIONS": options,
        }
        return config

    if scheme == "sqlite":
        name = unquote(parsed.path.lstrip("/")) or ":memory:"
        return {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": parsed.path.lstrip("/") or ":memory:",
        }

    raise ImproperlyConfigured(
        f"Unsupported DATABASE_URL scheme {scheme!r}. "
        "Use postgres:// or postgresql:// (sqlite:// is allowed for tests only)."
    )
