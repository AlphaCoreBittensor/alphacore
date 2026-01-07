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

# Environment profile (local | testing | production) ---------------------- #
# Controls default values across the validator configuration.
ENVIRONMENT = os.getenv("ALPHACORE_ENV", "local").strip().lower()

if ENVIRONMENT not in {"local", "testing", "production"}:
    ENVIRONMENT = "local"

# Treat the "testing" profile as testing mode by default, but allow an explicit
# ALPHACORE_TESTING override.
TESTING = _str_to_bool(
    os.getenv("ALPHACORE_TESTING", "true" if ENVIRONMENT == "testing" else "false")
)
DEBUG_MODE = _str_to_bool(os.getenv("ALPHACORE_DEBUG_MODE", "false"))

# Profile-specific defaults
if ENVIRONMENT == "local":
    ROUND_CADENCE_DEFAULT = 60  # seconds
    ROUND_SIZE_EPOCHS_DEFAULT = 1.0
    SAFETY_BUFFER_EPOCHS_DEFAULT = 0.02
    AVG_TASK_DURATION_DEFAULT = 300
    PRE_GENERATED_TASKS_DEFAULT = 5
    STOP_TASK_EVAL_DEFAULT = 0.80
    SKIP_ROUND_AFTER_DEFAULT = 0.95
elif ENVIRONMENT == "testing":
    ROUND_CADENCE_DEFAULT = 30  # seconds
    ROUND_SIZE_EPOCHS_DEFAULT = 0.347
    SAFETY_BUFFER_EPOCHS_DEFAULT = 0.02
    AVG_TASK_DURATION_DEFAULT = 300
    PRE_GENERATED_TASKS_DEFAULT = 0
    STOP_TASK_EVAL_DEFAULT = 0.65
    SKIP_ROUND_AFTER_DEFAULT = 0.95
else:  # production
    ROUND_CADENCE_DEFAULT = 30  # seconds
    ROUND_SIZE_EPOCHS_DEFAULT = 3.0
    SAFETY_BUFFER_EPOCHS_DEFAULT = 0.5
    AVG_TASK_DURATION_DEFAULT = 150
    PRE_GENERATED_TASKS_DEFAULT = 75
    STOP_TASK_EVAL_DEFAULT = 0.90
    SKIP_ROUND_AFTER_DEFAULT = 0.30

# Burn mechanism (reward distribution) ------------------------------------ #
# UID that receives burned tokens (default: UID 0)
BURN_UID = _env_int("ALPHACORE_BURN_UID", 0)
# Percentage of rewards to burn (default 0.9 = 90% burn, 10% to winner)
BURN_AMOUNT_PERCENTAGE = _env_float("ALPHACORE_BURN_AMOUNT_PERCENTAGE", 0.9)

# Validator identity and metadata ------------------------------------------ #

VALIDATOR_NAME = os.getenv("ALPHACORE_VALIDATOR_NAME", "alphacore-validator")
VALIDATOR_IMAGE = os.getenv("ALPHACORE_VALIDATOR_IMAGE", "alphacore:latest")
VALIDATOR_VERSION = os.getenv("ALPHACORE_VALIDATOR_VERSION", "0.1.0")

# Timings ------------------------------------------------------------------ #

ROUND_CADENCE_SECONDS = _env_int(
    "ALPHACORE_ROUND_CADENCE_SECONDS", ROUND_CADENCE_DEFAULT
)

# Round and epoch configuration -------------------------------------------- #

# Base round size and safety buffer (borrowed from autoppia defaults)
ROUND_SIZE_EPOCHS = _env_float("ALPHACORE_ROUND_SIZE_EPOCHS", ROUND_SIZE_EPOCHS_DEFAULT)
SAFETY_BUFFER_EPOCHS = _env_float(
    "ALPHACORE_SAFETY_BUFFER_EPOCHS", SAFETY_BUFFER_EPOCHS_DEFAULT
)

AVG_TASK_DURATION_SECONDS = _env_int(
    "ALPHACORE_AVG_TASK_DURATION_SECONDS", AVG_TASK_DURATION_DEFAULT
)

# Task generation and frequency -------------------------------------------- #

PRE_GENERATED_TASKS = _env_int(
    "ALPHACORE_PRE_GENERATED_TASKS", PRE_GENERATED_TASKS_DEFAULT
)
PROMPTS_PER_USECASE = _env_int(
    "ALPHACORE_PROMPTS_PER_USECASE", 3 if TESTING else 10
)
# Default to a single task per round. In epoch-aware mode (the default on test/finney),
# the validator is configured to start at most one round per epoch, making this effectively
# "one task per epoch" unless explicitly overridden.
TASKS_PER_ROUND = _env_int("ALPHACORE_TASKS_PER_ROUND", 1)
PROMPTS_PER_BATCH = _env_int("ALPHACORE_PROMPTS_PER_BATCH", 1)

# Round phase timing (fraction of round for different phases) --------------- #

STOP_TASK_EVALUATION_AT_ROUND_FRACTION = _env_float(
    "ALPHACORE_STOP_TASK_EVALUATION_AT_ROUND_FRACTION", STOP_TASK_EVAL_DEFAULT
)
FETCH_IPFS_AT_ROUND_FRACTION = _env_float(
    "ALPHACORE_FETCH_IPFS_AT_ROUND_FRACTION", 0.9
)

# Skip round if started too late (fraction of round already elapsed)
SKIP_ROUND_IF_STARTED_AFTER_FRACTION = _env_float(
    "ALPHACORE_SKIP_ROUND_IF_STARTED_AFTER_FRACTION", 0.95 if TESTING else 0.3
)

# Block synchronization ---------------------------------------------------- #

DZ_STARTING_BLOCK = _env_int("ALPHACORE_DZ_STARTING_BLOCK", 0)

# Miner query configuration ------------------------------------------------ #

MAX_MINERS_TO_QUERY = _env_int(
    "ALPHACORE_MAX_MINERS_TO_QUERY", 5 if TESTING else 50
)
MAX_DISPATCH_PER_ROUND = _env_int("ALPHACORE_MAX_DISPATCH_PER_ROUND", 64)
MIN_RESPONSE_RATE = _env_float("ALPHACORE_MIN_RESPONSE_RATE", 0.5)

# Concurrency limits ------------------------------------------------------ #

# Bound concurrent miner RPC calls (handshake/dispatch/feedback).
MINER_CONCURRENCY = _env_int("ALPHACORE_MINER_CONCURRENCY", 128)
# Bound concurrent validation API submissions.
#
# NOTE: The sandbox validation API instance we deploy with the validator only
# supports 4 concurrent requests reliably. Hard-cap here so operators cannot
# accidentally overload it via env vars.
VALIDATION_CONCURRENCY = min(4, _env_int("ALPHACORE_VALIDATION_CONCURRENCY", 4))

# Reward and scoring configuration ----------------------------------------- #

BASE_REWARD_SCORE = _env_float("ALPHACORE_BASE_REWARD_SCORE", 1.0)
QUALITY_THRESHOLD = _env_float("ALPHACORE_QUALITY_THRESHOLD", 0.7)
SCORE_WINDOW_SIZE = _env_int("ALPHACORE_SCORE_WINDOW_SIZE", 10)

# Latency and timeout scoring knobs ----------------------------------------- #

# Per-miner response timeout (seconds) applied to dendrite forward calls
if ENVIRONMENT == "local":
    _MINER_TIMEOUT_DEFAULT = 60
elif ENVIRONMENT == "testing":
    _MINER_TIMEOUT_DEFAULT = 10
else:
    _MINER_TIMEOUT_DEFAULT = 300

MINER_RESPONSE_TIMEOUT_SECONDS = _env_int(
    "ALPHACORE_MINER_RESPONSE_TIMEOUT_SECONDS", _MINER_TIMEOUT_DEFAULT
)

# Synapse-specific timeouts (seconds) -------------------------------------- #
#
# Historically, we used MINER_RESPONSE_TIMEOUT_SECONDS for both:
#   - StartRoundSynapse handshake (liveness probe)
#   - TaskSynapse dispatch (task execution)
#
# These have different needs: handshake should be short (skip dead miners fast),
# while task execution can be much longer.  We will potentially be switching to a push/poll model.
#
# Defaults:
#   - handshake: 5s
#   - task synapse: 1800s (30 minutes)
HANDSHAKE_TIMEOUT_SECONDS = _env_int("ALPHACORE_HANDSHAKE_TIMEOUT_SECONDS", 5)
TASK_SYNAPSE_TIMEOUT_SECONDS = _env_int(
    "ALPHACORE_TASK_SYNAPSE_TIMEOUT_SECONDS", 1800
)

# Enable latency-aware scoring where slower miners are penalized
LATENCY_SCORING_ENABLED = _str_to_bool(
    os.getenv("ALPHACORE_LATENCY_SCORING_ENABLED", "true" if TESTING else "false")
)

# Exponential decay factor for latency penalty: final = base * exp(-beta * latency_seconds)
TIME_WEIGHT_BETA = _env_float(
    "ALPHACORE_TIME_WEIGHT_BETA", 0.0 if TESTING else 0.01
)

# Combined scoring weights (API score + relative latency score).
# Final score (if latency scoring enabled):
#   final = api_weight * api_score + latency_weight * latency_score
# Where latency_score is in [0,1] based on relative latency within the round.
API_SCORE_WEIGHT = _env_float("ALPHACORE_API_SCORE_WEIGHT", 0.8)
LATENCY_SCORE_WEIGHT = _env_float("ALPHACORE_LATENCY_SCORE_WEIGHT", 0.2)
# Optional shaping for the relative latency score: score = (1 - normalized_delta) ** gamma
LATENCY_SCORE_GAMMA = _env_float("ALPHACORE_LATENCY_SCORE_GAMMA", 1.0)

# When latencies are extremely close (e.g., local test mode returning fixture zips instantly),
# normalized-delta scoring collapses to 1.0 for everyone because max_latency ~= min_latency.
# Enable a deterministic "tie spread" so the latency component still differentiates miners.
# This only applies when the observed latency range is <= LATENCY_TIE_EPSILON_S.
LATENCY_TIE_EPSILON_S = _env_float("ALPHACORE_LATENCY_TIE_EPSILON_S", 0.005)
# Maximum penalty applied (slowest miner gets 1 - penalty); fastest always gets 1.0.
LATENCY_TIE_PENALTY_MAX = _env_float("ALPHACORE_LATENCY_TIE_PENALTY_MAX", 0.1)

# Logging and monitoring --------------------------------------------------- #

LOG_LEVEL = os.getenv("ALPHACORE_LOG_LEVEL", "DEBUG" if TESTING else "INFO")
LOG_ROUND_SUMMARIES = _str_to_bool(
    os.getenv("ALPHACORE_LOG_ROUND_SUMMARIES", "true")
)
EXPORT_ROUND_STATS = _str_to_bool(
    os.getenv("ALPHACORE_EXPORT_ROUND_STATS", "false")
)
STATS_EXPORT_DIR = os.getenv("ALPHACORE_STATS_EXPORT_DIR", "./logs/stats")
VERBOSE_TASK_LOGGING = _str_to_bool(
    os.getenv("ALPHACORE_VERBOSE_TASK_LOGGING", "true" if TESTING else "false")
)
# While dispatching tasks with long timeouts, the validator can appear "stuck"
# in logs (it is just awaiting miner responses). Emit periodic progress logs so
# operators can distinguish a long-running call from a hang.
DISPATCH_PROGRESS_LOG_INTERVAL_S = _env_float(
    "ALPHACORE_DISPATCH_PROGRESS_LOG_INTERVAL_S", 30.0
)

# HTTP endpoint configuration ---------------------------------------------- #

ENABLE_HTTP_ENDPOINTS = _str_to_bool(
    os.getenv("ALPHACORE_ENABLE_HTTP_ENDPOINTS", "true")
)
HTTP_PORT = _env_int("ALPHACORE_HTTP_PORT", 8000)
HTTP_HOST = os.getenv("ALPHACORE_HTTP_HOST", "0.0.0.0")

# Validation API endpoint configuration ----------------------------------- #
# External validation service endpoint for scoring miner submissions
VALIDATION_API_ENABLED = _str_to_bool(
    os.getenv("ALPHACORE_VALIDATION_API_ENABLED", "true" if not TESTING else "false")
)
VALIDATION_API_ENDPOINT = _normalized(
    os.getenv("ALPHACORE_VALIDATION_API_ENDPOINT", "http://127.0.0.1:8888")
)
VALIDATION_API_TIMEOUT = _env_int(
    "ALPHACORE_VALIDATION_API_TIMEOUT", 300  # seconds - 5 minutes for validation to complete
)
VALIDATION_API_RETRIES = _env_int(
    "ALPHACORE_VALIDATION_API_RETRIES", 2
)

# Development and testing ------------------------------------------------- #

MOCK_VALIDATION = _str_to_bool(
    os.getenv("ALPHACORE_MOCK_VALIDATION", "true" if TESTING else "false")
)

# Checkpoint toggle (enable/disable persistence)
ENABLE_CHECKPOINT_SYSTEM = _str_to_bool(
    os.getenv("ALPHACORE_ENABLE_CHECKPOINT_SYSTEM", "true")
)

__all__ = [
    "TESTING",
    "DEBUG_MODE",
    "VALIDATOR_NAME",
    "VALIDATOR_IMAGE",
    "VALIDATOR_VERSION",
    "ROUND_CADENCE_SECONDS",
    "ROUND_SIZE_EPOCHS",
    "SAFETY_BUFFER_EPOCHS",
    "AVG_TASK_DURATION_SECONDS",
    "PRE_GENERATED_TASKS",
    "PROMPTS_PER_USECASE",
    "TASKS_PER_ROUND",
    "PROMPTS_PER_BATCH",
    "STOP_TASK_EVALUATION_AT_ROUND_FRACTION",
    "FETCH_IPFS_AT_ROUND_FRACTION",
    "SKIP_ROUND_IF_STARTED_AFTER_FRACTION",
    "DZ_STARTING_BLOCK",
    "MAX_MINERS_TO_QUERY",
    "MAX_DISPATCH_PER_ROUND",
    "MIN_RESPONSE_RATE",
    "MINER_CONCURRENCY",
    "VALIDATION_CONCURRENCY",
    "BASE_REWARD_SCORE",
    "QUALITY_THRESHOLD",
    "SCORE_WINDOW_SIZE",
    "MINER_RESPONSE_TIMEOUT_SECONDS",
    "HANDSHAKE_TIMEOUT_SECONDS",
    "TASK_SYNAPSE_TIMEOUT_SECONDS",
    "LATENCY_SCORING_ENABLED",
    "TIME_WEIGHT_BETA",
    "API_SCORE_WEIGHT",
    "LATENCY_SCORE_WEIGHT",
    "LATENCY_SCORE_GAMMA",
    "LOG_LEVEL",
    "LOG_ROUND_SUMMARIES",
    "EXPORT_ROUND_STATS",
    "STATS_EXPORT_DIR",
    "VERBOSE_TASK_LOGGING",
    "DISPATCH_PROGRESS_LOG_INTERVAL_S",
    "ENABLE_HTTP_ENDPOINTS",
    "HTTP_PORT",
    "HTTP_HOST",
    "MOCK_VALIDATION",
    "ENABLE_CHECKPOINT_SYSTEM",
    "VALIDATION_API_ENABLED",
    "VALIDATION_API_ENDPOINT",
    "VALIDATION_API_TIMEOUT",
    "VALIDATION_API_RETRIES",
]
