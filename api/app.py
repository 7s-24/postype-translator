from http.server import BaseHTTPRequestHandler
from openai import OpenAI
from bs4 import BeautifulSoup
import requests
import json
import os
import re
import time
import plistlib
import base64
import io
import urllib.parse

from api.db import (
    DatabaseNotConfigured,
    ValidationError,
    save_event,
    save_glossary_entries,
    save_glossary_upload,
    save_site_like,
)

# ---------------------------------------------------------------------------
# Models & config
# ---------------------------------------------------------------------------
STANDARD_MODELS = [
    # Text-first quality models from the user-provided free-quota pages.
    # "qwen-plus-2025-07-28",
    # "qwen3.6-plus",
    # "qwen-plus",
    # "qwen-max",
    # "qwen-max-2025-01-25",
    # "qwen3-max-preview",
    # "qwen3-next-80b-a3b-thinking",
    # "qwen3.5-35b-a3b",
    "qwen3-32b",
    "qwen3.5-27b",
    "qwen2.5-32b-instruct",
    "qwen3-14b",
    "qwen2.5-14b-instruct",
    "qwen2.5-14b-instruct-1m",

    # Vision-language models
    # "qwen3-vl-235b-a22b-thinking",
    "qwen-vl-plus-2025-05-07",
    "qwen2.5-vl-72b-instruct",
    "qwen-vl-plus-latest",
    "qwen2.5-vl-3b-instruct",
    # "qwen3-vl-30b-a3b-thinking",
    "qwen-vl-max-2025-08-13",
    "qwen-vl-plus",
    # "qwen3-vl-8b-thinking",
    "qwen3-vl-flash-2025-10-15",
    "qwen-vl-plus-2025-08-15",
    "qwen-vl-max-latest",
    "qwen3-vl-flash",
]

LIGHT_MODELS = [
    # Faster/cheaper text models; translation-specific flash is tried first.
    "qwen-mt-flash",
    "qwen3.6-flash",
    "qwen-turbo-latest",
    "qwen-turbo",
    "qwen3-coder-flash",
    "qwen3-8b",
    "qwen2.5-7b-instruct",
    "qwen3-0.6b",
]

# Backward-compatible defaults for callers/tests that pass a single model.
MODEL_QUALITY = STANDARD_MODELS[0]
MODEL_FAST    = LIGHT_MODELS[0]
MAX_CHARS     = 3000          # bigger chunks → fewer API calls
MODEL_STATE_FILE = os.getenv("MODEL_STATE_FILE", "/tmp/postype_translator_model_state.json")

ERRORS = {
    "MISSING_BODY": "缺少正文内容",
    "MISSING_INPUT": "缺少 URL 或内容",
    "MISSING_CHUNK": "缺少 chunk",
    "MISSING_TRANSLATED_TEXT": "缺少 translated_text",
    "MISSING_API_KEY": "服务器未配置 DASHSCOPE_API_KEY",
    "UNKNOWN_ACTION": "未知 action",
    "INTERNAL_ERROR": "服务器内部错误",
}

def err(code, status=400):
    return status, {
        "ok": False,
        "errorCode": code,
        # 不直接暴露给用户；前端可按 errorCode 显示友好提示
    }

# Models that are exposed through the OpenAI-compatible chat endpoint but reject
# a dedicated system role. Keep prompts as a single user message for them.
USER_ONLY_ROLE_MODEL_PREFIXES = (
    "qwen-mt-",
)

def model_uses_user_only_messages(model: str) -> bool:
    return any((model or "").startswith(prefix) for prefix in USER_ONLY_ROLE_MODEL_PREFIXES)

def build_chat_messages(system_prompt: str, user_prompt: str, model: str) -> list:
    if model_uses_user_only_messages(model):
        return [{"role": "user", "content": f"{system_prompt}\n\n{user_prompt}"}]
    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]

def is_bad_request_error(exc: Exception) -> bool:
    return getattr(exc, "status_code", None) == 400

def friendly_provider_error(exc: Exception) -> str:
    if is_bad_request_error(exc) and not is_quota_error(exc):
        return (
            "当前模型无法处理该请求，可能是不支持当前消息格式或参数。"
            "请切换模型/关闭快速模式后重试。"
        )
    return "翻译服务暂时不可用，请稍后重试。"

# ---------------------------------------------------------------------------
# System prompts
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """你是专业韩文同人小说翻译器，负责将韩文正文翻译成简体中文。

【核心要求】
- 忠实翻译原文，不删减、不总结、不规避、不弱化。
- 保留原文的情绪、暧昧张力、人物关系、语气词、停顿、喘息、拟声词。
- 不添加原文没有的信息。
- 不解释，不加注释，不输出前缀。
- 只输出译文。

【文风要求】
- 所有内容按原文翻译，不额外加重，也不弱化。
- 不确定说话人时，不要擅自添加主语。
- 原文故意省略主语时，中文也可以适度省略。
"""

EXTRACT_TERMS_PROMPT = """你是专业韩文小说术语提取器。请阅读以下韩文小说文本，提取所有专有名词和术语。

重点关注：
1. 带 「」『』[]〈〉《》"" '' 及其他引号或括号包裹的词（通常是技能名、称号、物品名、作品名等）
2. 反复出现的人名（包括昵称、绰号、缩写）、地名、组织名
3. 虚构概念、魔法/能力名称、头衔、职位
4. 非日常用语的专有名词

输出要求：
- 只输出 JSON 数组，不要任何其他文字、markdown 标记或代码块符号
- 每项格式：{"ko": "韩文原文", "zh": "建议中文翻译", "category": "类别"}
- category 可选值：人名、地名、技能、称号、物品、组织、其他
- 不要包含日常词汇和通用动词/形容词
- 如果没有找到术语，返回空数组 []
"""

FIX_SYSTEM_PROMPT = """你是专业韩文同人小说翻译器，负责修正已经翻译文本中的韩文残留。

【修正要求】
- 只翻译文本中的韩文部分，保留其余已经是中文的内容原样。
- 不要解释、不加注释、不输出前缀。
- 不要改写本已是中文的部分。
- 尽量参考上下文保持说话风格一致。
- 只返回修正后的结果。
"""


# ---------------------------------------------------------------------------
# HTML / WebArchive parsing
# ---------------------------------------------------------------------------

def parse_webarchive(data: bytes) -> str:
    plist = plistlib.load(io.BytesIO(data))
    main_resource = plist.get("WebMainResource", {})
    data_bytes = main_resource.get("WebResourceData")
    if isinstance(data_bytes, bytes):
        return data_bytes.decode("utf-8", errors="replace")
    raise ValueError("Invalid webarchive format or no main resource")


def parse_postype_html(html: str, section_id: str = "post-content") -> str:
    soup = BeautifulSoup(html, "lxml")
    content = soup.find(id=section_id)
    if content is None:
        raise RuntimeError(f"没有找到 id='{section_id}'，请检查上传的 HTML 是否为 Postype 页面。")
    for tag in content.find_all(["script", "style", "button", "nav", "aside"]):
        tag.decompose()
    text = content.get_text("\n", strip=True)
    text = re.sub(r"\n{3,}", "\n\n", text)
    if not text.strip():
        raise RuntimeError("正文为空。")
    return text.strip()


def fetch_postype_text(url: str) -> str:
    headers = {"User-Agent": "Mozilla/5.0 Chrome/122.0 Safari/537.36"}
    resp = requests.get(url, headers=headers, timeout=30)
    resp.raise_for_status()
    return parse_postype_html(resp.text)


# ---------------------------------------------------------------------------
# Text chunking
# ---------------------------------------------------------------------------

def split_text(text: str, max_chars: int = MAX_CHARS):
    paragraphs = text.replace("\r\n", "\n").split("\n")
    chunks, current = [], ""
    for para in paragraphs:
        para = para.strip()
        if not para:
            continue
        if len(current) + len(para) + 2 <= max_chars:
            current += para + "\n\n"
        else:
            if current.strip():
                chunks.append(current.strip())
            if len(para) > max_chars:
                for i in range(0, len(para), max_chars):
                    chunks.append(para[i : i + max_chars])
                current = ""
            else:
                current = para + "\n\n"
    if current.strip():
        chunks.append(current.strip())
    return chunks


# ---------------------------------------------------------------------------
# Glossary helpers
# ---------------------------------------------------------------------------

def sample_text(text: str, max_chars: int = 10000) -> str:
    if len(text) <= max_chars:
        return text
    first = text[:5000]
    mid_start = len(text) // 2 - 1250
    middle = text[mid_start : mid_start + 2500]
    last = text[-2500:]
    return first + "\n…\n" + middle + "\n…\n" + last


def extract_terms(client, text: str, model: str = MODEL_QUALITY) -> list:
    sampled = sample_text(text)
    try:
        resp = client.chat.completions.create(
            model=model,
            messages=build_chat_messages(EXTRACT_TERMS_PROMPT, sampled, model),
            temperature=0.1,
        )
        raw = resp.choices[0].message.content.strip()
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
        terms = json.loads(raw)
        if not isinstance(terms, list):
            return []
        valid = []
        for t in terms:
            if isinstance(t, dict) and "ko" in t and "zh" in t:
                valid.append({
                    "ko": str(t["ko"]),
                    "zh": str(t["zh"]),
                    "category": str(t.get("category", "其他")),
                })
        return valid
    except Exception as exc:
        if is_quota_error(exc):
            raise
        return []


def build_glossary_prompt_section(glossary: list) -> str:
    if not glossary:
        return ""
    lines = ["【术语表——必须严格遵守以下译法，不得自行另译】"]
    for item in glossary:
        ko, zh = item.get("ko", ""), item.get("zh", "")
        if ko and zh:
            lines.append(f"{ko} → {zh}")
    return "\n".join(lines)


def apply_glossary_to_text(text: str, glossary: list) -> str:
    if not glossary:
        return text
    sorted_g = sorted(glossary, key=lambda g: len(g.get("ko", "")), reverse=True)
    for item in sorted_g:
        ko, zh = item.get("ko", ""), item.get("zh", "")
        if ko and zh and ko in text:
            text = text.replace(ko, zh)
    return text


# ---------------------------------------------------------------------------
# Translation
# ---------------------------------------------------------------------------

def translate_by_google(text: str) -> str:
    try:
        url = "https://translate.googleapis.com/translate_a/single"
        params = {"client": "gtx", "sl": "ko", "tl": "zh-CN", "dt": "t", "q": text[:5000]}
        headers = {"User-Agent": "Mozilla/5.0 Chrome/122.0 Safari/537.36"}
        resp = requests.get(url, params=params, headers=headers, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            if isinstance(data, list) and isinstance(data[0], list):
                return "".join(seg[0] for seg in data[0] if seg[0])
    except Exception:
        pass
    return text


def is_quota_error(exc: Exception) -> bool:
    """Best-effort detection for DashScope/OpenAI-compatible quota exhaustion."""
    status_code = getattr(exc, "status_code", None)
    code = str(getattr(exc, "code", "") or "").lower()
    message = str(exc).lower()
    quota_markers = (
        "quota",
        "free quota",
        "insufficient_quota",
        "insufficient quota",
        "exceeded",
        "balance",
        "billing",
        "no enough",
        "credit",
    )
    if status_code in (402, 429) and any(m in message for m in quota_markers):
        return True
    return any(m in code or m in message for m in quota_markers)


def split_chunk_further(chunk: str, max_chars: int = 800) -> list:
    lines = chunk.replace("\r", "\n").split("\n")
    sub_chunks, current = [], ""
    for line in lines:
        line = line.strip()
        if not line:
            continue
        if len(current) + len(line) + 1 <= max_chars:
            current = (current + "\n" + line) if current else line
        else:
            if current:
                sub_chunks.append(current)
            current = line
    if current:
        sub_chunks.append(current)
    return sub_chunks if sub_chunks else [chunk]


def translate_chunk(
    client, chunk, index, total,
    previous_translation="",
    glossary=None,
    model=MODEL_QUALITY,
    retry_count=0,
):
    context = ""
    if previous_translation:
        context = (
            "【上一段译文结尾，仅用于保持上下文一致，不要重复翻译】\n"
            f"{previous_translation[-2000:]}\n\n"
        )

    glossary_section = build_glossary_prompt_section(glossary) if glossary else ""
    if glossary_section:
        glossary_section += "\n\n"

    user_prompt = (
        f"{context}{glossary_section}"
        f"下面是韩文小说正文的第 {index}/{total} 段。\n\n"
        "请直接翻译成简体中文。注意承接上一段的人物称呼、语气、情绪和文风，"
        "但不要重复上一段内容。\n"
        "特别注意保持术语和专有名词的翻译一致性，"
        "如果术语表中有对应条目，必须使用术语表中的译法。\n\n"
        f"【当前原文】\n{chunk}\n"
    )

    try:
        response = client.chat.completions.create(
            model=model,
            messages=build_chat_messages(SYSTEM_PROMPT, user_prompt, model),
            temperature=0.2,
        )
        return response.choices[0].message.content.strip()
    except Exception as exc:
        if is_quota_error(exc):
            raise
        if retry_count == 0:
            sub_chunks = split_chunk_further(chunk)
            if len(sub_chunks) > 1:
                results = []
                for sc in sub_chunks:
                    try:
                        results.append(
                            translate_chunk(
                                client, sc, index, total,
                                previous_translation, glossary,
                                model=model, retry_count=1,
                            )
                        )
                    except Exception as exc:
                        if is_quota_error(exc):
                            raise
                        fb = translate_by_google(sc)
                        results.append(apply_glossary_to_text(fb, glossary or []))
                return "\n".join(results)
        fallback = translate_by_google(chunk)
        fallback = apply_glossary_to_text(fallback, glossary or [])
        if fallback and fallback != chunk:
            return fallback
        raise


# ---------------------------------------------------------------------------
# Korean-residue fixer
# ---------------------------------------------------------------------------

def contains_korean(text: str) -> bool:
    return bool(re.search(r"[\u3131-\u318E\uAC00-\uD7A3]", text))


def fix_korean_line(client, line, previous="", next_line="", model=MODEL_QUALITY):
    prompt = (
        "下面是一段已经翻译成中文的文本，其中仍有韩文残留。\n"
        "请只翻译文本中的韩文部分为简体中文，保留其他已经是中文的内容原样。\n"
        "不要添加注释、说明、前缀或额外内容。不要改写已是中文的部分。\n\n"
        f"上一行（仅供参考）：{previous}\n"
        f"当前行：{line}\n"
        f"下一行（仅供参考）：{next_line}\n"
    )
    response = client.chat.completions.create(
        model=model,
        messages=build_chat_messages(FIX_SYSTEM_PROMPT, prompt, model),
        temperature=0.2,
    )
    return response.choices[0].message.content.strip()


def fix_korean_text(client, text, model=MODEL_QUALITY):
    lines = text.splitlines()
    fixed = []
    for idx, line in enumerate(lines):
        if contains_korean(line):
            prev = lines[idx - 1] if idx > 0 else ""
            nxt = lines[idx + 1] if idx < len(lines) - 1 else ""
            fixed.append(fix_korean_line(client, line, prev, nxt, model=model))
        else:
            fixed.append(line)
    return "\n".join(fixed)


# ---------------------------------------------------------------------------
# HTTP handler (Vercel serverless)
# ---------------------------------------------------------------------------

class handler(BaseHTTPRequestHandler):
    def _send_json(self, status, data):
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False).encode("utf-8"))

    def do_GET(self):
        self._send_json(200, {"ok": True, "message": "Postype translator API is running."})

    def do_OPTIONS(self):
        self._send_json(200, {"ok": True})

    def _get_client(self):
        api_key = os.getenv("DASHSCOPE_API_KEY")
        if not api_key:
            return None
        return OpenAI(
            base_url="https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
            api_key=api_key,
        )

    def _tier_name(self, data):
        return "light" if data.get("fast") else "standard"

    def _models_for_tier(self, tier):
        return LIGHT_MODELS if tier == "light" else STANDARD_MODELS

    def _load_model_state(self):
        try:
            with open(MODEL_STATE_FILE, "r", encoding="utf-8") as f:
                state = json.load(f)
        except Exception:
            state = {}

        for tier in ("standard", "light"):
            tier_state = state.get(tier) if isinstance(state.get(tier), dict) else {}
            tier_state.setdefault("currentIndex", 0)
            tier_state.setdefault("exhaustedModels", [])
            state[tier] = tier_state
        return state

    def _save_model_state(self, state):
        try:
            state_dir = os.path.dirname(MODEL_STATE_FILE)
            if state_dir:
                os.makedirs(state_dir, exist_ok=True)
            with open(MODEL_STATE_FILE, "w", encoding="utf-8") as f:
                json.dump(state, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def _current_model_status(self, tier):
        models = self._models_for_tier(tier)
        state = self._load_model_state()
        tier_state = state[tier]
        exhausted = set(tier_state.get("exhaustedModels", []))
        if len(exhausted) >= len(models):
            exhausted = set()
            tier_state["exhaustedModels"] = []

        start = int(tier_state.get("currentIndex", 0)) % len(models)
        current_index = start
        for offset in range(len(models)):
            idx = (start + offset) % len(models)
            if models[idx] not in exhausted:
                current_index = idx
                break

        tier_state["currentIndex"] = current_index
        self._save_model_state(state)
        return {
            "tier": tier,
            "model": models[current_index],
            "currentIndex": current_index,
            "models": models,
            "exhaustedModels": list(tier_state.get("exhaustedModels", [])),
        }

    def _ordered_models(self, tier):
        status = self._current_model_status(tier)
        models = status["models"]
        exhausted = set(status["exhaustedModels"])
        start = status["currentIndex"]
        active = [
            models[(start + offset) % len(models)]
            for offset in range(len(models))
            if models[(start + offset) % len(models)] not in exhausted
        ]
        return active or models

    def _mark_model_exhausted(self, tier, model):
        models = self._models_for_tier(tier)
        state = self._load_model_state()
        tier_state = state[tier]
        exhausted = tier_state.setdefault("exhaustedModels", [])
        if model not in exhausted:
            exhausted.append(model)

        for offset in range(1, len(models) + 1):
            idx = (models.index(model) + offset) % len(models)
            if models[idx] not in exhausted:
                tier_state["currentIndex"] = idx
                break
        else:
            tier_state["currentIndex"] = 0
        self._save_model_state(state)

    def _run_with_model_rotation(self, tier, callback, rotate_on_bad_request=False):
        models = self._ordered_models(tier)
        first_model = models[0]
        last_exc = None

        for model in models:
            try:
                result = callback(model)
                status = self._current_model_status(tier)
                return result, {
                    "tier": tier,
                    "model": model,
                    "switchedModel": model != first_model,
                    "currentModel": status["model"],
                    "exhaustedModels": status["exhaustedModels"],
                }
            except Exception as exc:
                if is_quota_error(exc):
                    last_exc = exc
                    self._mark_model_exhausted(tier, model)
                    continue
                if rotate_on_bad_request and is_bad_request_error(exc):
                    last_exc = exc
                    continue
                raise

        if last_exc:
            raise last_exc
        raise RuntimeError("没有可用模型")

    def _pick_model(self, data):
        return self._current_model_status(self._tier_name(data))["model"]

    def _db_payload(self, data):
        payload = data.get("payload")
        return payload if isinstance(payload, dict) else data

    def _send_db_result(self, callback):
        try:
            result = callback()
            return self._send_json(200, {"ok": True, "data": result})
        except DatabaseNotConfigured as exc:
            message = str(exc) or "MongoDB 未配置，请设置 MONGODB_URI 和 MONGODB_DB_NAME"
            return self._send_json(503, {"ok": False, "error": message})
        except ValidationError as exc:
            return self._send_json(400, {"ok": False, "error": str(exc)})

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
            data = json.loads(body.decode("utf-8"))
            action = data.get("action", "")

            # === MODEL STATUS ===
            if action == "model_status":
                tier = self._tier_name(data)
                status = self._current_model_status(tier)
                return self._send_json(200, {"ok": True, **status})

            # === PREPARE ===
            if action == "prepare":
                file_data = data.get("fileData")
                if file_data:
                    if file_data.get("type") == "webarchive":
                        binary = base64.b64decode(file_data["content"])
                        html = parse_webarchive(binary)
                    else:
                        html = file_data["content"]
                    original_text = parse_postype_html(html)
                elif data.get("text"):
                    original_text = data["text"].strip()
                    if not original_text:
                        return self._send_json(400, {"error": "缺少正文内容"})
                else:
                    url = data.get("url", "").strip()
                    if not url:
                        return self._send_json(400, {"error": "缺少 URL 或内容"})
                    original_text = fetch_postype_text(url)

                chunks = split_text(original_text)
                return self._send_json(200, {
                    "ok": True, "chunks": chunks, "total": len(chunks),
                })

            # === EXTRACT TERMS ===
            if action == "extract_terms":
                text = data.get("text", "")
                if not text:
                    return self._send_json(200, {"ok": True, "terms": []})
                client = self._get_client()
                if not client:
                    return self._send_json(500, {"error": "服务器未配置 DASHSCOPE_API_KEY"})
                # Term extraction is a quality-sensitive one-shot step, so use
                # the standard model pool with quota-aware rotation.
                terms, meta = self._run_with_model_rotation(
                    "standard",
                    lambda model: extract_terms(client, text, model=model),
                )
                return self._send_json(200, {"ok": True, "terms": terms, **meta})

            # === MONGODB OPTIONAL WRITES ===
            if action == "record_like":
                payload = self._db_payload(data)
                return self._send_db_result(lambda: save_site_like(payload))

            if action == "save_glossary_upload":
                payload = self._db_payload(data)
                return self._send_db_result(lambda: save_glossary_upload(payload))

            if action == "save_glossary_entries":
                payload = self._db_payload(data)
                entries = payload.get("entries", [])
                context = payload.get("context", {}) if isinstance(payload.get("context"), dict) else payload
                return self._send_db_result(lambda: save_glossary_entries(entries, context))

            if action == "track_event":
                payload = self._db_payload(data)
                return self._send_db_result(lambda: save_event(payload))

            # === TRANSLATE ===
            if action == "translate":
                chunk = data.get("chunk", "")
                index = int(data.get("index", 1))
                total = int(data.get("total", 1))
                previous = data.get("previous", "")
                glossary = data.get("glossary", [])
                tier = self._tier_name(data)

                if not chunk:
                    return self._send_json(400, {"error": "缺少 chunk"})
                client = self._get_client()
                if not client:
                    return self._send_json(500, {"error": "服务器未配置 DASHSCOPE_API_KEY"})

                try:
                    translated, meta = self._run_with_model_rotation(
                        tier,
                        lambda model: translate_chunk(
                            client, chunk, index, total, previous,
                            glossary=glossary, model=model,
                        ),
                    )
                    return self._send_json(200, {
                        "ok": True, "translated": translated, "fallback": False, **meta,
                    })
                except Exception:
                    translated = translate_by_google(chunk)
                    translated = apply_glossary_to_text(translated, glossary)
                    return self._send_json(200, {
                        "ok": True, "translated": translated, "fallback": True,
                        "note": "此 chunk 使用了机械翻译",
                    })

            # === FIX ===
            if action == "fix":
                translated_text = data.get("translated_text", "")
                if not translated_text:
                    return self._send_json(400, {"error": "缺少 translated_text"})
                client = self._get_client()
                if not client:
                    return self._send_json(500, {"error": "服务器未配置 DASHSCOPE_API_KEY"})
                tier = self._tier_name(data)
                fixed, meta = self._run_with_model_rotation(
                    tier,
                    lambda model: fix_korean_text(client, translated_text, model=model),
                    rotate_on_bad_request=True,
                )
                return self._send_json(200, {"ok": True, "fixed_text": fixed, **meta})

            self._send_json(400, {"error": "未知 action"})

        except Exception as e:
            if is_bad_request_error(e) and not is_quota_error(e):
                return self._send_json(400, {"ok": False, "error": friendly_provider_error(e)})
            self._send_json(500, {"ok": False, "error": friendly_provider_error(e)})
