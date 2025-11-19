"""Environment-driven configuration for AlphaCore validators."""

from __future__ import annotations

import os
from typing import Any, Callable

from dotenv import load_dotenv

load_dotenv()


def _str_to_bool(value: str) -> bool:
    return value.lower() in {"1", "true", "yes", "on"}


def _env_int(key: str, default: int) -> int:
    try:
        return int(os.getenv(key, default))
    except (TypeError, ValueError):
        return default


def _env_float(key: str, default: float) -> float:
    try:
        return float(os.getenv(key, default))
    except (TypeError, ValueError):
        return default


def _normalized(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _env_value(key: str, cast: Callable[[str], Any], default: Any) -> Any:
    raw = os.getenv(key)
    if raw is None:
        return default
    try:
        return cast(raw)
    except Exception:
        return default


# High-level toggles ------------------------------------------------------- #

TESTING = _str_to_bool(os.getenv("ALPHACORE_TESTING", "false"))

# Timings ------------------------------------------------------------------ #

ROUND_CADENCE_SECONDS = _env_int(
    "ALPHACORE_ROUND_CADENCE_SECONDS", 60 if TESTING else 600
)
TASK_TIMEOUT_SECONDS = _env_int(
    "ALPHACORE_TASK_TIMEOUT_SECONDS", 30 if TESTING else 60
)

# Misc configuration ------------------------------------------------------- #

PROMPTS_PER_BATCH = _env_int("ALPHACORE_PROMPTS_PER_BATCH", 1)
MAX_DISPATCH_PER_ROUND = _env_int("ALPHACORE_MAX_DISPATCH_PER_ROUND", 64)

__all__ = [
    "TESTING",
    "ROUND_CADENCE_SECONDS",
    "TASK_TIMEOUT_SECONDS",
    "PROMPTS_PER_BATCH",
    "MAX_DISPATCH_PER_ROUND",
]
