"""Runtime configuration helpers shared by API handlers."""

import os
import time

MAX_CHARS = 3000  # bigger chunks → fewer API calls
MODEL_STATE_FILE = os.getenv("MODEL_STATE_FILE", "/tmp/postype_translator_model_state.json")


def env_float(name: str, default: float) -> float:
    value = os.getenv(name)
    if value is None:
        return default
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None:
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= 0 else default


# DashScope/OpenAI-compatible HTTP timeouts. The defaults intentionally stay
# well below Vercel's 300s runtime limit so a half-open provider connection can
# fail into the existing model-rotation/fallback paths instead of exhausting the
# whole serverless invocation.
DASHSCOPE_CONNECT_TIMEOUT_SEC = env_float("DASHSCOPE_CONNECT_TIMEOUT_SEC", 10.0)
DASHSCOPE_READ_TIMEOUT_SEC = env_float("DASHSCOPE_READ_TIMEOUT_SEC", 45.0)
DASHSCOPE_WRITE_TIMEOUT_SEC = env_float("DASHSCOPE_WRITE_TIMEOUT_SEC", 10.0)
DASHSCOPE_POOL_TIMEOUT_SEC = env_float("DASHSCOPE_POOL_TIMEOUT_SEC", 10.0)
DASHSCOPE_MAX_RETRIES = env_int("DASHSCOPE_MAX_RETRIES", 0)

# Per-handler time budgets (within Vercel's 300s ceiling).
HANDLER_TOTAL_BUDGET_SEC = env_float("HANDLER_TOTAL_BUDGET_SEC", 240.0)
STREAM_HANDLER_BUDGET_SEC = env_float("STREAM_HANDLER_BUDGET_SEC", 170.0)
FIX_HANDLER_BUDGET_SEC = env_float("FIX_HANDLER_BUDGET_SEC", 210.0)
SENSITIVE_FALLBACK_BUDGET_SEC = env_float("SENSITIVE_FALLBACK_BUDGET_SEC", 35.0)
GOOGLE_RESERVE_SEC = env_float("GOOGLE_RESERVE_SEC", 25.0)
GOOGLE_REQUEST_TIMEOUT_SEC = env_float("GOOGLE_REQUEST_TIMEOUT_SEC", 10.0)
DEADLINE_SHUTDOWN_BUFFER_SEC = env_float("DEADLINE_SHUTDOWN_BUFFER_SEC", 5.0)


def deadline_remaining(deadline):
    if deadline is None:
        return None
    return deadline - time.monotonic()


def deadline_exceeded(deadline):
    return deadline is not None and time.monotonic() >= deadline
