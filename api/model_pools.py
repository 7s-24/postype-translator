"""Model pool definitions for translation fallback and rotation."""

STANDARD_MODELS = [
    "qwen3-next-80b-a3b-instruct",
    "qwen-plus-2025-09-11",
    "qwen3-30b-a3b-instruct-2507",
    "qwen-plus-2025-07-14",
    "qwen3-235b-a22b-instruct-2507",
    "deepseek-v3.2",
    "qwen-plus-2025-04-28",
    "qwen-plus-latest",
    "qwen3-max-2026-01-23",
    "qwen3-max",
    "qwen3-max-2025-09-23",
    "qwen3-32b",
    "qwen3-235b-a22b",
    "qwen3-30b-a3b-thinking-2507",
    "qwen3-235b-a22b-thinking-2507",
    "qwen-vl-plus",
    "qwen3-vl-plus",
    "qwen3-vl-plus-2025-12-19",
    "qwen3-vl-plus-2025-09-23",
    "qwen3-vl-235b-a22b-instruct",
]

LIGHT_MODELS = [
    "deepseek-v4-flash",
    "qwen-flash-2025-07-28",
    "qwen3-0.6b",
    "qwen3-8b",
    "qwen-mt-lite",
]

SENSITIVE_FALLBACK_MODELS = [
    "qwen-mt-flash",
    "qwen-mt-turbo",
    "qwen-turbo",
    "qwen-flash",
    "qwen-vl-max",
    "qwen-vl-plus",
    "qwen3-vl-flash-2025-10-15",
    "qwen3-vl-flash-2026-01-22",
    "qwen3-vl-flash",
    "qwen3-30b-a3b-instruct-2507",
    "qwen3-next-80b-a3b-instruct",
    "qwen3-vl-30b-a3b-instruct",
    "qwen3-vl-8b-instruct",
]

# Backward-compatible defaults for callers/tests that pass a single model.
MODEL_QUALITY = STANDARD_MODELS[0]
MODEL_FAST = LIGHT_MODELS[0]
