"""MongoDB data-access helpers for optional server-side persistence.

The translator remains usable without MongoDB. Callers should catch
``DatabaseNotConfigured`` when they invoke write helpers in environments where
``MONGODB_URI`` or ``MONGODB_DB_NAME`` is not configured.
"""

from datetime import datetime, timezone
import importlib
import os
from urllib.parse import urlparse

MONGODB_URI_ENV = "MONGODB_URI"
MONGODB_DB_NAME_ENV = "MONGODB_DB_NAME"

_client = None
_pymongo = None


class DatabaseNotConfigured(RuntimeError):
    """Raised when MongoDB environment variables are missing."""


class ValidationError(ValueError):
    """Raised when whitelisted payload data is missing required fields."""


def _utcnow():
    return datetime.now(timezone.utc)


def _require_string(payload, key, max_length, *, required=True):
    value = payload.get(key)
    if value is None:
        if required:
            raise ValidationError(f"缺少字段: {key}")
        return None
    value = str(value).strip()
    if not value:
        if required:
            raise ValidationError(f"字段不能为空: {key}")
        return None
    return value[:max_length]


def _optional_int(payload, key, *, minimum=None, maximum=None):
    if payload.get(key) is None:
        return None
    try:
        value = int(payload[key])
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"字段必须是整数: {key}") from exc
    if minimum is not None:
        value = max(minimum, value)
    if maximum is not None:
        value = min(maximum, value)
    return value


def _normalize_url(url):
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValidationError("pageUrl 必须是有效的 http(s) URL")
    return url[:2048]


def _serialize_value(value):
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, list):
        return [_serialize_value(item) for item in value]
    if isinstance(value, dict):
        return _serialize_document(value)
    return value


def _serialize_document(document):
    if not document:
        return document
    serialized = {}
    for key, value in dict(document).items():
        if key == "_id":
            serialized["id"] = str(value)
        else:
            serialized[key] = _serialize_value(value)
    return serialized


def get_db():
    """Return the configured MongoDB database.

    The client and pymongo module are initialized lazily so importing this
    module does not require MongoDB configuration and does not affect existing
    localStorage-only usage.
    """
    global _client, _pymongo
    uri = os.getenv(MONGODB_URI_ENV)
    db_name = os.getenv(MONGODB_DB_NAME_ENV)
    if not uri or not db_name:
        raise DatabaseNotConfigured("MongoDB 未配置")
    if _pymongo is None:
        if importlib.util.find_spec("pymongo") is None:
            raise DatabaseNotConfigured("pymongo 未安装，请先安装 requirements.txt")
        _pymongo = importlib.import_module("pymongo")
    if _client is None:
        _client = _pymongo.MongoClient(uri, serverSelectionTimeoutMS=5000)
    return _client[db_name]


def get_collection(name):
    """Return a named MongoDB collection from the configured database."""
    return get_db()[name]


def sanitize_site_like(payload):
    """Whitelist and normalize a site-like write payload."""
    page_url = _normalize_url(_require_string(payload, "pageUrl", 2048))
    delta = _optional_int(payload, "delta", minimum=-1, maximum=1)
    if delta is None:
        delta = 1
    sanitized = {
        "pageUrl": page_url,
        "pageTitle": _require_string(payload, "pageTitle", 300, required=False),
        "targetId": _require_string(payload, "targetId", 120, required=False),
        "source": _require_string(payload, "source", 80, required=False),
        "userId": _require_string(payload, "userId", 120, required=False),
        "delta": delta,
    }
    return {k: v for k, v in sanitized.items() if v is not None}


def save_site_like(payload):
    """Increment the like counter for a page using only whitelisted fields."""
    doc = sanitize_site_like(payload)
    now = _utcnow()
    result = get_collection("site_likes").find_one_and_update(
        {"pageUrl": doc["pageUrl"], "targetId": doc.get("targetId")},
        {
            "$setOnInsert": {
                "pageUrl": doc["pageUrl"],
                "targetId": doc.get("targetId"),
                "createdAt": now,
            },
            "$set": {
                "pageTitle": doc.get("pageTitle"),
                "source": doc.get("source"),
                "updatedAt": now,
            },
            "$inc": {"likeCount": doc["delta"]},
        },
        upsert=True,
        return_document=True,
    )
    return _serialize_document(result)


def sanitize_glossary_entry(payload):
    """Whitelist and normalize one glossary term entry."""
    sanitized = {
        "ko": _require_string(payload, "ko", 300),
        "zh": _require_string(payload, "zh", 300),
        "category": _require_string(payload, "category", 50, required=False) or "其他",
        "note": _require_string(payload, "note", 500, required=False),
        "source": _require_string(payload, "source", 120, required=False),
    }
    return {k: v for k, v in sanitized.items() if v is not None}


def _sanitize_entries(entries, *, max_entries=1000):
    if not isinstance(entries, list):
        raise ValidationError("entries 必须是数组")
    return [sanitize_glossary_entry(entry) for entry in entries[:max_entries] if isinstance(entry, dict)]


def sanitize_glossary_upload(payload):
    """Whitelist and normalize a user-uploaded glossary draft."""
    entries = _sanitize_entries(payload.get("entries", []))
    if not entries:
        raise ValidationError("术语库至少需要一条有效 entries")
    sanitized = {
        "userId": _require_string(payload, "userId", 120, required=False),
        "sourceUrl": _require_string(payload, "sourceUrl", 2048, required=False),
        "sourceTitle": _require_string(payload, "sourceTitle", 300, required=False),
        "locale": _require_string(payload, "locale", 40, required=False) or "ko-zh-CN",
        "submitterNickname": _require_string(payload, "submitterNickname", 80, required=False),
        "notes": _require_string(payload, "notes", 1000, required=False),
        "status": "draft",
        "entries": entries,
        "entryCount": len(entries),
    }
    if sanitized.get("sourceUrl"):
        sanitized["sourceUrl"] = _normalize_url(sanitized["sourceUrl"])
    return {k: v for k, v in sanitized.items() if v is not None}


def save_glossary_upload(payload):
    """Insert a user-uploaded glossary draft with whitelisted fields only."""
    doc = sanitize_glossary_upload(payload)
    now = _utcnow()
    doc.update({"createdAt": now, "updatedAt": now})
    result = get_collection("glossary_uploads").insert_one(doc)
    doc["_id"] = result.inserted_id
    return _serialize_document(doc)


def save_glossary_entries(entries, context=None):
    """Insert reviewed public glossary entries with whitelisted fields only."""
    context = context or {}
    sanitized_entries = _sanitize_entries(entries)
    if not sanitized_entries:
        raise ValidationError("至少需要一条有效术语")
    now = _utcnow()
    shared = {
        "status": "approved",
        "sourceUploadId": _require_string(context, "sourceUploadId", 120, required=False),
        "sourceUrl": _require_string(context, "sourceUrl", 2048, required=False),
        "reviewedBy": _require_string(context, "reviewedBy", 120, required=False),
        "createdAt": now,
        "updatedAt": now,
    }
    if shared.get("sourceUrl"):
        shared["sourceUrl"] = _normalize_url(shared["sourceUrl"])
    shared = {k: v for k, v in shared.items() if v is not None}
    docs = [{**entry, **shared} for entry in sanitized_entries]
    result = get_collection("glossary_entries").insert_many(docs)
    for doc, inserted_id in zip(docs, result.inserted_ids):
        doc["_id"] = inserted_id
    return [_serialize_document(doc) for doc in docs]


def sanitize_event(payload):
    """Whitelist and normalize a lightweight operational event."""
    event_type = _require_string(payload, "eventType", 80)
    sanitized = {
        "eventType": event_type,
        "pageUrl": _require_string(payload, "pageUrl", 2048, required=False),
        "source": _require_string(payload, "source", 80, required=False),
        "tier": _require_string(payload, "tier", 40, required=False),
        "model": _require_string(payload, "model", 120, required=False),
        "durationMs": _optional_int(payload, "durationMs", minimum=0, maximum=86_400_000),
        "chunkCount": _optional_int(payload, "chunkCount", minimum=0, maximum=100_000),
        "termCount": _optional_int(payload, "termCount", minimum=0, maximum=100_000),
        "ok": bool(payload["ok"]) if "ok" in payload else None,
    }
    if sanitized.get("pageUrl"):
        sanitized["pageUrl"] = _normalize_url(sanitized["pageUrl"])
    return {k: v for k, v in sanitized.items() if v is not None}


def save_event(payload):
    """Insert a whitelisted lightweight operational event."""
    doc = sanitize_event(payload)
    doc["createdAt"] = _utcnow()
    result = get_collection("events").insert_one(doc)
    doc["_id"] = result.inserted_id
    return _serialize_document(doc)
