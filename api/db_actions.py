"""Dispatch helpers for optional MongoDB-backed write actions."""

from api.db import (
    save_event,
    save_glossary_entries,
    save_glossary_upload,
    save_site_like,
)


def db_payload(data):
    """Return the nested database payload when present, otherwise the body."""
    payload = data.get("payload")
    return payload if isinstance(payload, dict) else data


def build_db_write(action, data):
    """Return a zero-argument callback for a supported DB write action."""
    payload = db_payload(data)

    if action == "record_like":
        return lambda: save_site_like(payload)

    if action == "save_glossary_upload":
        return lambda: save_glossary_upload(payload)

    if action == "save_glossary_entries":
        entries = payload.get("entries", [])
        context = payload.get("context", {}) if isinstance(payload.get("context"), dict) else payload
        return lambda: save_glossary_entries(entries, context)

    if action == "track_event":
        return lambda: save_event(payload)

    return None
