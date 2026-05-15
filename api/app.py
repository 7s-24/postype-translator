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
import random
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

ERROR_ACTION = "如果方便的话，可以复制以下的错误码，并描述错误产生的情况，提交给 fedrick1plela755@gmail.com 来帮助改进："

ERRORS = {
    "MISSING_BODY": "Missing body",
    "MISSING_INPUT": "Missing URL or content",
    "MISSING_CHUNK": "Missing chunk",
    "MISSING_TRANSLATED_TEXT": "Missing translated_text",
    "MISSING_API_KEY": "Server is missing DASHSCOPE_API_KEY",
    "UNKNOWN_ACTION": "Unknown action",
    "DATABASE_NOT_CONFIGURED": "Database is not configured",
    "VALIDATION_ERROR": "Invalid database payload",
    "PROVIDER_BAD_REQUEST": "The selected model could not process this request",
    "PROVIDER_UNAVAILABLE": "Translation service is temporarily unavailable",
    "INTERNAL_ERROR": "Internal server error",
}

def error_response(code, status=400, message=None):
    return status, {
        "ok": False,
        "error": {
            "code": code,
            "message": message or ERRORS.get(code, ERRORS["INTERNAL_ERROR"]),
            "action": ERROR_ACTION,
        },
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
        return ERRORS["PROVIDER_BAD_REQUEST"]
    return ERRORS["PROVIDER_UNAVAILABLE"]

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

FIX_SYSTEM_PROMPT = """你是专业韩文同人小说翻译器，负责对已经翻译成中文的文本做最小必要修正。

【修正范围】
- 必须修正文本中的韩文残留，把残留韩文翻译成简体中文。
- 对照韩文原文和术语表，只检查句子中逻辑明显奇怪、称呼明显不一致或与术语表冲突的部分，修正疑似术语误译；不要借机重译通顺的句子。
- 如果文本来自自动/谷歌翻译，请重点核对人称、称呼、说话对象和主语关系，修正明显的“我/你/他/她/他们/她们”等人称错误。
- 无法从原文和上下文明确判断的问题，保持现有中文不变。

【输出要求】
- 保留其余已经正确的中文内容原样，尽量保留原有换行和段落结构。
- 不要解释、不加注释、不输出前缀。
- 只返回修正后的中文结果。
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


def filter_glossary_for_chunk(glossary: list, chunk: str) -> list:
    if not chunk:
        return glossary
    return [
        item for item in glossary
        if item.get("ko") and item["ko"] in chunk
    ]


def build_glossary_prompt_section(glossary: list, chunk=None) -> str:
    if chunk is not None:
        glossary = filter_glossary_for_chunk(glossary, chunk)
    if not glossary:
        return ""
    lines = ["【术语表——必须严格遵守以下译法，不得自行另译】"]
    for item in glossary:
        ko, zh = item.get("ko", ""), item.get("zh", "")
        if ko and zh:
            lines.append(f"{ko} → {zh}")
    return "\n".join(lines)


def preprocess_source_with_glossary(source: str, glossary: list) -> str:
    if not glossary:
        return source
    sorted_g = sorted(glossary, key=lambda g: len(g.get("ko", "")), reverse=True)
    for item in sorted_g:
        ko, zh = item.get("ko", ""), item.get("zh", "")
        if ko and zh:
            source = source.replace(ko, zh)
    return source


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

    glossary_section = build_glossary_prompt_section(glossary, chunk=chunk) if glossary else ""
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
                        fallback_source = preprocess_source_with_glossary(sc, glossary or [])
                        results.append(translate_by_google(fallback_source))
                return "\n".join(results)
        fallback_source = preprocess_source_with_glossary(chunk, glossary or [])
        fallback = translate_by_google(fallback_source)
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


def fix_translation_chunk(
    client,
    source_text,
    translated_text,
    previous_translation="",
    next_translation="",
    glossary=None,
    used_fallback=False,
    index=1,
    total=1,
    model=MODEL_QUALITY,
):
    glossary_section = build_glossary_prompt_section(glossary, chunk=source_text) if glossary else ""
    fallback_note = (
        "该段曾使用自动/谷歌翻译兜底，请特别核对人称、称呼、说话对象和主语关系。"
        if used_fallback else
        "该段不一定来自自动翻译；如无明确错误，请尽量保持现有中文。"
    )
    prompt = (
        f"下面是第 {index}/{total} 段的韩文原文和当前中文译文。请做最小必要修正。\n"
        f"{fallback_note}\n\n"
        f"{glossary_section}\n\n"
        "【上一段中文译文，仅供判断人称和称呼】\n"
        f"{previous_translation[-1200:]}\n\n"
        "【韩文原文】\n"
        f"{source_text}\n\n"
        "【当前中文译文】\n"
        f"{translated_text}\n\n"
        "【下一段中文译文，仅供判断人称和称呼】\n"
        f"{next_translation[:1200]}\n\n"
        "请只输出修正后的当前中文译文。"
    )
    response = client.chat.completions.create(
        model=model,
        messages=build_chat_messages(FIX_SYSTEM_PROMPT, prompt, model),
        temperature=0.2,
    )
    return response.choices[0].message.content.strip()


def fix_translated_chunks(
    client,
    source_chunks,
    translated_chunks,
    fallback_indices=None,
    glossary=None,
    model=MODEL_QUALITY,
):
    fallback_set = set(fallback_indices or [])
    fixed = list(translated_chunks)
    total = len(fixed)

    for idx, translated in enumerate(translated_chunks):
        chunk_no = idx + 1
        used_fallback = chunk_no in fallback_set
        if not contains_korean(translated) and not used_fallback:
            continue

        source = source_chunks[idx] if idx < len(source_chunks) else ""
        previous_translation = fixed[idx - 1] if idx > 0 else ""
        next_translation = translated_chunks[idx + 1] if idx < total - 1 else ""
        fixed[idx] = fix_translation_chunk(
            client,
            source,
            translated,
            previous_translation=previous_translation,
            next_translation=next_translation,
            glossary=glossary,
            used_fallback=used_fallback,
            index=chunk_no,
            total=total,
            model=model,
        )

    return "\n\n".join(fixed)


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

    def _ordered_models(self, tier, model_session_id=None):
        status = self._current_model_status(tier)
        models = status["models"]
        exhausted = set(status["exhaustedModels"])

        if model_session_id:
            active = [model for model in models if model not in exhausted]
            if active:
                rng = random.Random(f"{tier}:{model_session_id}")
                rng.shuffle(active)
                return active
            return models

        start = status["currentIndex"]
        active = [
            models[(start + offset) % len(models)]
            for offset in range(len(models))
            if models[(start + offset) % len(models)] not in exhausted
        ]
        return active or models

    def _model_session_id(self, data):
        value = data.get("modelSessionId") or data.get("model_session_id")
        if value is None:
            return None
        value = str(value).strip()
        return value[:200] or None

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

    def _run_with_model_rotation(self, tier, callback, rotate_on_bad_request=False, model_session_id=None):
        models = self._ordered_models(tier, model_session_id=model_session_id)
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
                    "modelOrder": models,
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
            message = str(exc) or ERRORS["DATABASE_NOT_CONFIGURED"]
            status, payload = error_response("DATABASE_NOT_CONFIGURED", 503, message)
            return self._send_json(status, payload)
        except ValidationError as exc:
            message = str(exc) or ERRORS["VALIDATION_ERROR"]
            status, payload = error_response("VALIDATION_ERROR", 400, message)
            return self._send_json(status, payload)

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
            data = json.loads(body.decode("utf-8"))
            action = data.get("action", "")

            # === MODEL STATUS ===
            if action == "model_status":
                tier = self._tier_name(data)
                model_session_id = self._model_session_id(data)
                status = self._current_model_status(tier)
                if model_session_id:
                    status["modelOrder"] = self._ordered_models(tier, model_session_id=model_session_id)
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
                        status, payload = error_response("MISSING_BODY", 400)
                        return self._send_json(status, payload)
                else:
                    url = data.get("url", "").strip()
                    if not url:
                        status, payload = error_response("MISSING_INPUT", 400)
                        return self._send_json(status, payload)
                    original_text = fetch_postype_text(url)

                chunks = split_text(original_text)
                return self._send_json(200, {
                    "ok": True, "chunks": chunks, "total": len(chunks),
                })

            # === EXTRACT TERMS ===
            if action == "extract_terms":
                model_session_id = self._model_session_id(data)
                text = data.get("text", "")
                if not text:
                    return self._send_json(200, {"ok": True, "terms": []})
                client = self._get_client()
                if not client:
                    status, payload = error_response("MISSING_API_KEY", 500)
                    return self._send_json(status, payload)
                # Term extraction is a quality-sensitive one-shot step, so use
                # the standard model pool with quota-aware rotation.
                terms, meta = self._run_with_model_rotation(
                    "standard",
                    lambda model: extract_terms(client, text, model=model),
                    model_session_id=model_session_id,
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
                model_session_id = self._model_session_id(data)
                chunk = data.get("chunk", "")
                index = int(data.get("index", 1))
                total = int(data.get("total", 1))
                previous = data.get("previous", "")
                glossary = data.get("glossary", [])
                tier = self._tier_name(data)

                if not chunk:
                    status, payload = error_response("MISSING_CHUNK", 400)
                    return self._send_json(status, payload)
                client = self._get_client()
                if not client:
                    status, payload = error_response("MISSING_API_KEY", 500)
                    return self._send_json(status, payload)

                try:
                    translated, meta = self._run_with_model_rotation(
                        tier,
                        lambda model: translate_chunk(
                            client, chunk, index, total, previous,
                            glossary=glossary, model=model,
                        ),
                        model_session_id=model_session_id,
                    )
                    return self._send_json(200, {
                        "ok": True, "translated": translated, "fallback": False, **meta,
                    })
                except Exception:
                    fallback_source = preprocess_source_with_glossary(chunk, glossary or [])
                    translated = translate_by_google(fallback_source)
                    return self._send_json(200, {
                        "ok": True, "translated": translated, "fallback": True,
                        "note": "此 chunk 使用了机械翻译",
                        "modelOrder": self._ordered_models(tier, model_session_id=model_session_id),
                    })

            # === FIX ===
            if action == "fix":
                model_session_id = self._model_session_id(data)
                translated_text = data.get("translated_text", "")
                if not translated_text:
                    status, payload = error_response("MISSING_TRANSLATED_TEXT", 400)
                    return self._send_json(status, payload)
                client = self._get_client()
                if not client:
                    status, payload = error_response("MISSING_API_KEY", 500)
                    return self._send_json(status, payload)
                tier = self._tier_name(data)
                source_chunks = data.get("source_chunks", [])
                translated_chunks = data.get("translated_chunks", [])
                fallback_indices = data.get("fallback_indices", [])
                glossary = data.get("glossary", [])
                if not isinstance(fallback_indices, list):
                    fallback_indices = []

                if isinstance(source_chunks, list) and isinstance(translated_chunks, list) and translated_chunks:
                    fixed, meta = self._run_with_model_rotation(
                        tier,
                        lambda model: fix_translated_chunks(
                            client,
                            [str(chunk) for chunk in source_chunks],
                            [str(chunk) for chunk in translated_chunks],
                            fallback_indices=[int(i) for i in fallback_indices if str(i).isdigit()],
                            glossary=glossary,
                            model=model,
                        ),
                        rotate_on_bad_request=True,
                        model_session_id=model_session_id,
                    )
                else:
                    fixed, meta = self._run_with_model_rotation(
                        tier,
                        lambda model: fix_korean_text(client, translated_text, model=model),
                        rotate_on_bad_request=True,
                        model_session_id=model_session_id,
                    )
                return self._send_json(200, {"ok": True, "fixed_text": fixed, **meta})

            status, payload = error_response("UNKNOWN_ACTION", 400)
            self._send_json(status, payload)

        except Exception as e:
            if is_bad_request_error(e) and not is_quota_error(e):
                status, payload = error_response("PROVIDER_BAD_REQUEST", 400, friendly_provider_error(e))
                return self._send_json(status, payload)
            status, payload = error_response("PROVIDER_UNAVAILABLE", 500, friendly_provider_error(e))
            self._send_json(status, payload)
