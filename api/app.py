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

from api.db import DatabaseNotConfigured, ValidationError
from api.db_actions import build_db_write

# ---------------------------------------------------------------------------
# Models & config
# ---------------------------------------------------------------------------
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
    "qwen3-14b",
    "qwen3.6-35b-a3b",
    "qwen3.5-122b-a10b",
    "deepseek-v4-pro",
    "qwen3-30b-a3b-thinking-2507",
    "qwen3-235b-a22b-thinking-2507",
    "qwen3.5-35b-a3b",
    "qwen3-30b-a3b",
    "qwq-plus",
    "qwen3.5-27b",
    # "qwen3.5-plus",
    # "qwen3.6-plus",
    # "qwen3.6-plus-2026-04-02",
    # "qwen3.5-397b-a17b",
    # "qwen3.5-plus-2026-02-15",
    "qwen-vl-plus",
    "qwen3-vl-plus",
    "qwen3-vl-plus-2025-12-19",
    "qwen3-vl-plus-2025-09-23",
    "qwen3-vl-235b-a22b-instruct",
    "qwen3-vl-8b-thinking",
    "qwen3-vl-235b-a22b-thinking",
]

LIGHT_MODELS = [
    "deepseek-v4-flash",
    "qwen-flash-2025-07-28",
    "qwen3-0.6b",
    "qwen3-8b",
    "qwen-mt-lite",
    "qwen3.6-flash-2026-04-16",
    "qwen3.6-flash",
    "qwen3.5-flash-2026-02-23",
    # "qwen3.5-flash",
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
    "qwen3-vl-235b-a22b-thinking",
    "qwen3-vl-30b-a3b-instruct",
    "qwen3-vl-30b-a3b-thinking",
    "qwen3-vl-8b-instruct",
    # "qwen3.5-plus-2026-04-20",
    # "qwen3.6-27b",
    # "qwen3.6-max-preview",
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
    "RESTRICTED_POST_CONTENT": "这是付费/受限内容，请利用浏览器的阅读模式复制后手动输入，或通过浏览器保存 HTML（WebArchive 功能上线中）再重试翻译。",
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

# ---------------------------------------------------------------------------
# Model family detection
# ---------------------------------------------------------------------------
# Qwen-MT 系列是阿里云专门做的翻译模型：
#   - messages 只能有一条 user，content 必须是纯源文
#   - 不支持 system / 多轮 / temperature
#   - 语言、术语必须通过 extra_body.translation_options 传递
QWEN_MT_MODEL_PREFIXES = ("qwen-mt-",)

# 保留旧名作为别名，避免外部引用断裂
USER_ONLY_ROLE_MODEL_PREFIXES = QWEN_MT_MODEL_PREFIXES


def is_qwen_mt_model(model: str) -> bool:
    return any((model or "").startswith(prefix) for prefix in QWEN_MT_MODEL_PREFIXES)


def model_uses_user_only_messages(model: str) -> bool:
    # 保留旧名供已有代码调用
    return is_qwen_mt_model(model)


# Deepseek 系列不识别 /no_think，原样输出会被当作普通文本忽略
# 但为了干净，仍然只给 qwen3 / qwq 系列追加
def model_supports_no_think(model: str) -> bool:
    model = (model or "").lower()
    if not model:
        return False
    if is_qwen_mt_model(model):
        return False
    return model.startswith("qwen") or model.startswith("qwq")


NO_THINK_SUFFIX = "\n\n/no_think"


def build_chat_messages(system_prompt: str, user_prompt: str, model: str) -> list:
    if is_qwen_mt_model(model):
        return [{"role": "user", "content": user_prompt}]

    suffix = NO_THINK_SUFFIX if model_supports_no_think(model) else ""
    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt + suffix},
    ]


def build_qwen_mt_request(chunk: str, glossary: list = None) -> dict:
    """构造 Qwen-MT 专用的请求参数。
    """
    translation_options = {
        "source_lang": "Korean",
        "target_lang": "Chinese",
    }

    if glossary:
        relevant = filter_glossary_for_chunk(glossary, chunk)
        terms = [
            {"source": item["ko"], "target": item["zh"]}
            for item in relevant
            if item.get("ko") and item.get("zh")
        ]
        if terms:
            translation_options["terms"] = terms

    return {
        "messages": [{"role": "user", "content": chunk}],
        "extra_body": {"translation_options": translation_options},
    }


def is_bad_request_error(exc: Exception) -> bool:
    return getattr(exc, "status_code", None) == 400

def friendly_provider_error(exc: Exception) -> str:
    if is_bad_request_error(exc) and not is_quota_error(exc):
        return ERRORS["PROVIDER_BAD_REQUEST"]
    return ERRORS["PROVIDER_UNAVAILABLE"]

# ---------------------------------------------------------------------------
# System prompts（仅给非 mt 模型使用）
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """你是韩译中同人小说翻译器。

<rules>
- 忠实翻译原文，保留情绪、张力、语气词、拟声词、停顿、喘息。
- 不删减、不总结、不规避、不弱化、不添加原文没有的信息。
- 原文省略主语时，中文也省略；不擅自补主语。
</rules>

<output>仅输出简体中文译文，无前缀、无注释、无解释。</output>
"""

EXTRACT_TERMS_PROMPT = """你是韩文小说术语翻译器。系统已给出候选词，你只在候选范围内筛选并翻译。

<rules>
- 保留：人名、昵称、称呼、地名、组织、头衔、物品、作品名、虚构概念，以及需统一译法的反复出现实词。
- 标注【引号内出现】的词条优先保留，仅当明显是普通台词强调时才过滤。
- 过滤：连接词、助词/语尾残片、代词、泛用副词/动词/形容词、数字量词、普通寒暄、普通身体部位/家具/日常名词（除非作昵称或专名）。
- 候选词若被助词粘连，ko 字段写干净的原形/词干。
- 不确定时过滤掉。
- 不要从上下文新增候选词。
</rules>

<output>
仅输出 JSON 数组，无 markdown、无代码块、无其他文字。
每项格式：{"ko":"韩文","zh":"中文","category":"人名|地名|技能|称号|物品|组织|称呼|其他"}
无可用词条时返回 []。
</output>
"""

FIX_SYSTEM_PROMPT = """你是中文译文最小修正器。

<rules>
- 把残留韩文翻译成简体中文。
- 对照术语表，仅修正与术语表冲突或明显误译的专名/称呼。
- 仅修正与原文/上下文明显冲突的人称（我/你/他/她/他们/她们）。
- 删除谷歌翻译多余补出的主语（原文无主语且中文省略自然时）。
- 其余通顺的中文一律保留原样，保留换行和段落结构。
- 无法从原文和上下文明确判断时，保持现有中文不变。
</rules>

<output>仅输出修正后的中文，无前缀、无注释、无解释。</output>
"""

SIMPLE_FALLBACK_FIX_SYSTEM_PROMPT = """你是机翻中文的极窄修正器。

<rules>
- 仅修正：与术语表冲突的专名/称呼。
- 仅修正：与原文/上下文明显冲突的人称。
- 仅删除：谷歌翻译多余补出的主语。
- 不重译、不润色、不补写、不分析敏感描写本身。
- 无法明确判断时，保持原样。
</rules>

<output>仅输出修正后的中文，无前缀、无注释、无解释。</output>
"""


# ---------------------------------------------------------------------------
# HTML / WebArchive parsing
# ---------------------------------------------------------------------------

RESTRICTED_POST_CONTENT_MAX_CHARS = 160
RESTRICTED_POST_CONTENT_MESSAGE = (
    "这是付费/受限内容，请利用浏览器的阅读模式复制后手动输入，"
    "或通过浏览器保存 HTML（WebArchive 功能上线中）再重试翻译。"
)
RESTRICTED_POST_CONTENT_PATTERN_GROUPS = (
    ("성인용 콘텐츠입니다",),
    ("본인 인증 완료", "성인물 열람을 허용"),
    ("여기서부터는 포스트 구매자만 볼 수 있어요",),
    ("지금 포인트로 결제하고 포스트를 계속 감상해 보세요",),
)


class RestrictedPostContentError(RuntimeError):
    pass


def normalize_text_for_access_check(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def is_restricted_post_content(text: str) -> bool:
    normalized = normalize_text_for_access_check(text)
    if len(normalized) > RESTRICTED_POST_CONTENT_MAX_CHARS:
        return False
    return any(
        all(pattern in normalized for pattern in group)
        for group in RESTRICTED_POST_CONTENT_PATTERN_GROUPS
    )


def ensure_accessible_post_content(text: str) -> str:
    if is_restricted_post_content(text):
        raise RestrictedPostContentError(RESTRICTED_POST_CONTENT_MESSAGE)
    return text


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
    text = text.strip()
    if not text:
        raise RuntimeError("正文为空。")
    return ensure_accessible_post_content(text)


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

KOREAN_CONTENT_TOKEN_RE = re.compile(r"[가-힣]{2,}")

# 韩文小说常用的成对引号/括号。每对 (左, 右) 都会被用于抽取内部内容。
QUOTED_TERM_DELIMITERS = (
    ("「", "」"),
    ("『", "』"),
    ("《", "》"),
    ("〈", "〉"),
    ("【", "】"),
    ("［", "］"),
    ("[", "]"),
    ("\u201c", "\u201d"),   # “”
    ("\u2018", "\u2019"),   # ‘’
)

# 内部必须至少含一个韩文字符，才算韩文术语候选；
# 允许内部混入数字/汉字/空格，但整体长度有上限，避免把一整句台词当术语。
QUOTED_TERM_MAX_INNER_LEN = 20
QUOTED_TERM_HAS_KOREAN_RE = re.compile(r"[가-힣]")
QUOTED_TERM_SENTENCE_PUNCT = "。！？.!?…"

# 构造一个总的正则：匹配任意一种成对引号内的内容（非贪婪，不跨行）
_QUOTED_TERM_PATTERN = "|".join(
    f"{re.escape(l)}([^{re.escape(l)}{re.escape(r)}\\n]{{1,{QUOTED_TERM_MAX_INNER_LEN}}}?){re.escape(r)}"
    for l, r in QUOTED_TERM_DELIMITERS
)
QUOTED_TERM_RE = re.compile(_QUOTED_TERM_PATTERN)


def extract_quoted_terms(text: str) -> list:
    """从成对引号/括号中抽取候选术语。

    返回 [(token, count), ...]，按出现次数和首次出现顺序排序。
    引号内的内容至少要包含一个韩文字符；会自动去除首尾空白；
    会跳过明显是整句台词的内容（带句末标点或过长）。
    """
    if not text:
        return []

    counts = {}
    first_seen = {}
    order = 0

    for match in QUOTED_TERM_RE.finditer(text):
        inner = next((g for g in match.groups() if g is not None), None)
        if not inner:
            continue
        token = inner.strip()
        if len(token) < 2:
            continue
        # 必须含韩文；纯数字、纯英文、纯标点都跳过
        if not QUOTED_TERM_HAS_KOREAN_RE.search(token):
            continue
        # 整句话过滤：包含句末标点的，大概率是台词不是术语
        if any(ch in token for ch in QUOTED_TERM_SENTENCE_PUNCT):
            continue
        counts[token] = counts.get(token, 0) + 1
        if token not in first_seen:
            first_seen[token] = order
            order += 1

    return sorted(
        counts.items(),
        key=lambda item: (-item[1], -len(item[0]), first_seen[item[0]]),
    )


KOREAN_STOPWORDS = {
    "그리고", "그러나", "하지만", "그래서", "그러면", "그러니까", "그런데", "그러다",
    "이렇게", "그렇게", "저렇게", "어떻게", "이제", "다시", "이미", "아직", "바로",
    "너무", "정말", "진짜", "아주", "조금", "잠깐", "계속", "그냥", "어서", "빨리",
    "모두", "전부", "자꾸", "가장", "항상", "절대", "역시", "물론", "혹시", "분명",
    "그것", "이것", "저것", "여기", "거기", "저기", "누구", "무엇", "뭐야", "어디",
    "내가", "네가", "제가", "나는", "너는", "우린", "우리는", "너희", "자신",
    "있다", "없다", "했다", "한다", "하면", "하고", "하는", "해서", "됐다", "된다", "되어",
    "보다", "보는", "봤다", "왔다", "가는", "갔다", "같다", "같은", "싶다", "싶은",
    "말했다", "말한", "생각", "정도", "사람", "시간", "때문", "지금", "오늘", "어제",
}

KOREAN_PARTICLE_SUFFIXES = (
    "에게서", "으로서", "으로써", "로부터", "까지", "부터", "처럼", "보다", "마다",
    "조차", "마저", "라도", "이나", "나마", "에게", "한테", "께서", "에서", "으로",
    "하고", "이랑", "랑", "과", "와", "은", "는", "이", "가", "을", "를", "에", "의",
    "도", "만", "로", "야", "아", "여",
)

TERM_CANDIDATE_LIMIT = 24
TERM_CONTEXT_SAMPLE_CHARS = 2000

# 这些词通常可以被模型直接翻译，不需要进入术语审核；
# 引号内出现的同形词仍会保留给模型判断，避免误删专名/称号。
KOREAN_EASY_TRANSLATABLE_TERMS = {
    "머리", "머릿속", "머리카락", "눈", "눈동자", "눈길", "시선", "얼굴", "표정",
    "코", "입", "입술", "귀", "목", "목소리", "어깨", "팔", "손", "손가락",
    "가슴", "허리", "다리", "발", "발목", "몸", "등", "피부", "침대", "문",
    "창문", "방", "집", "책상", "의자", "소파", "옷", "신발",
}


def sample_text(text: str, max_chars: int = 10000) -> str:
    """按 max_chars 自适应切首/中/尾。

    首 50% / 中 25% / 尾 25%，对任何 max_chars 都成立；
    原文不超过 max_chars 时原样返回。
    """
    if len(text) <= max_chars:
        return text
    first_len = max_chars // 2
    side_len = max_chars // 4
    first = text[:first_len]
    mid_start = max(0, len(text) // 2 - side_len // 2)
    middle = text[mid_start : mid_start + side_len]
    last = text[-side_len:]
    return first + "\n…\n" + middle + "\n…\n" + last


def normalize_korean_content_token(token: str) -> str:
    token = (token or "").strip()
    if len(token) < 2 or token in KOREAN_STOPWORDS:
        return ""
    normalized = token
    changed = True
    while changed and len(normalized) > 2:
        changed = False
        for suffix in KOREAN_PARTICLE_SUFFIXES:
            if normalized.endswith(suffix) and len(normalized) - len(suffix) >= 2:
                normalized = normalized[: -len(suffix)]
                changed = True
                break
    if len(normalized) < 2 or normalized in KOREAN_STOPWORDS:
        return ""
    return normalized


def extract_frequent_content_words(text: str, limit: int = TERM_CANDIDATE_LIMIT) -> list:
    # ---- 引号术语：即使只出现一次也保留 ----
    quoted = extract_quoted_terms(text or "")
    quoted_tokens = {token for token, _ in quoted}

    # ---- 原有的频率统计 ----
    counts = {}
    first_seen = {}
    order = 0
    for match in KOREAN_CONTENT_TOKEN_RE.finditer(text or ""):
        token = normalize_korean_content_token(match.group(0))
        if not token or token in KOREAN_EASY_TRANSLATABLE_TERMS:
            continue
        counts[token] = counts.get(token, 0) + 1
        if token not in first_seen:
            first_seen[token] = order
            order += 1

    if not counts and not quoted:
        return []

    min_count = 3 if len(text or "") >= 5000 else 2
    candidates = [(token, count) for token, count in counts.items() if count >= min_count]
    if len(candidates) < 12:
        candidates = [(token, count) for token, count in counts.items() if count >= 2]

    candidates.sort(key=lambda item: (-item[1], -len(item[0]), first_seen[item[0]]))

    # ---- 合并：引号术语优先放在前面，避免被 limit 截掉 ----
    merged = list(quoted)
    seen = set(quoted_tokens)
    for token, count in candidates:
        if token in seen:
            continue
        merged.append((token, count))
        seen.add(token)

    return merged[:limit]


def build_term_translation_prompt(text: str, candidates: list) -> str:
    quoted_tokens = {token for token, _ in extract_quoted_terms(text or "")}
    candidate_lines = []
    for token, count in candidates:
        suffix = "（引号内出现）" if token in quoted_tokens else f"（{count}次）"
        candidate_lines.append(f"- {token}{suffix}")
    sampled = sample_text(text, max_chars=TERM_CONTEXT_SAMPLE_CHARS)
    return (
        "<candidates>\n"
        + "\n".join(candidate_lines)
        + "\n</candidates>\n\n"
        "<context_sample>\n"
        + sampled
        + "\n</context_sample>"
    )


def extract_terms(client, text: str, model: str = MODEL_QUALITY) -> list:
    # Qwen-MT 只会做翻译，无法完成"过滤 + 分类 + 输出 JSON"的复合任务
    if is_qwen_mt_model(model):
        raise RuntimeError(f"extract_terms 不支持 Qwen-MT 模型: {model}")

    candidates = extract_frequent_content_words(text)
    if not candidates:
        return []
    prompt = build_term_translation_prompt(text, candidates)
    resp = client.chat.completions.create(
        model=model,
        messages=build_chat_messages(EXTRACT_TERMS_PROMPT, prompt, model),
        temperature=0.1,
    )
    raw = resp.choices[0].message.content.strip()
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)
    try:
        terms = json.loads(raw)
    except Exception:
        return []
    if not isinstance(terms, list):
        return []
    valid = []
    quoted_tokens = {token for token, _ in extract_quoted_terms(text or "")}
    for t in terms:
        if isinstance(t, dict) and "ko" in t and "zh" in t:
            ko = str(t["ko"]).strip()
            zh = str(t["zh"]).strip()
            if not ko or not zh:
                continue
            if ko in KOREAN_EASY_TRANSLATABLE_TERMS and ko not in quoted_tokens:
                continue
            valid.append({
                "ko": ko,
                "zh": zh,
                "category": str(t.get("category", "其他")),
            })
    return valid


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

class TranslationText(str):
    def __new__(cls, value: str, used_google: bool = False):
        obj = str.__new__(cls, value)
        obj.used_google = used_google
        return obj


class StreamingTranslationInterrupted(Exception):
    """Raised when a streaming provider call fails after tokens reached client."""


class StreamingClientGoneError(Exception):
    """Raised when the client closed the SSE connection (page unloaded, etc.)."""


class StreamingTimeoutError(Exception):
    """Raised when streaming has no token for too long; treat as soft failure."""


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


def translate_by_google_with_glossary(text: str, glossary: list) -> str:
    glossary = glossary or []
    prepared = preprocess_source_with_glossary(text, glossary)
    translated = translate_by_google(prepared)
    return apply_glossary_to_text(translated, glossary)


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


def is_sensitive_content_error(exc: Exception) -> bool:
    """Best-effort detection for provider content moderation/safety refusals."""
    status_code = getattr(exc, "status_code", None)
    code = str(getattr(exc, "code", "") or "").lower()
    message = str(exc).lower()
    body = str(getattr(exc, "body", "") or "").lower()
    combined = " ".join([code, message, body])
    sensitive_markers = (
        "sensitive",
        "content policy",
        "content_policy",
        "content-policy",
        "safety",
        "safe guard",
        "safeguard",
        "moderation",
        "moderated",
        "audit",
        "review failed",
        "risk content",
        "risky content",
        "unsafe",
        "refuse",
        "refusal",
        "rejected by policy",
        "violat",
        "prohibited",
        "not allowed",
        "inappropriate",
        "illegal",
        "敏感",
        "内容安全",
        "安全",
        "审核",
        "审查",
        "风控",
        "违规",
        "违反",
        "拒绝",
        "不合规",
        "不安全",
        "禁止",
        "高风险",
    )
    if status_code in (400, 403, 422) and any(m in combined for m in sensitive_markers):
        return True
    return any(m in code or m in message or m in body for m in sensitive_markers)


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


def build_translation_user_prompt(chunk, index, total, previous_translation="", glossary=None):
    """非 mt 模型的翻译 user prompt（XML 结构化）。"""
    parts = []
    if previous_translation:
        parts.append(
            "<previous_translation>\n"
            f"{previous_translation[-2000:]}\n"
            "</previous_translation>"
        )
    if glossary:
        gloss_section = build_glossary_prompt_section(glossary, chunk=chunk)
        if gloss_section:
            parts.append(f"<glossary>\n{gloss_section}\n</glossary>")
    parts.append(
        f"<task>翻译第 {index}/{total} 段韩文，承接上文称呼/语气/文风，术语必须用 glossary 中的译法。</task>"
    )
    parts.append(f"<source>\n{chunk}\n</source>")
    return "\n\n".join(parts)


def translate_chunk(
    client, chunk, index, total,
    previous_translation="",
    glossary=None,
    model=MODEL_QUALITY,
    retry_count=0,
    allow_google_fallback=True,
    enable_internal_retry=True,
):
    try:
        if is_qwen_mt_model(model):
            # mt 系列：纯源文 + translation_options，舍弃上下文（mt 不支持多轮）
            mt_kwargs = build_qwen_mt_request(chunk, glossary=glossary)
            response = client.chat.completions.create(model=model, **mt_kwargs)
        else:
            user_prompt = build_translation_user_prompt(
                chunk, index, total, previous_translation, glossary,
            )
            response = client.chat.completions.create(
                model=model,
                messages=build_chat_messages(SYSTEM_PROMPT, user_prompt, model),
                temperature=0.2,
            )
        return response.choices[0].message.content.strip()
    except Exception as exc:
        if is_quota_error(exc) or is_sensitive_content_error(exc):
            raise
        # 被外层模型轮换调用时，禁用内部切小重试；失败立刻 raise，让外层换模型，
        # 避免一个 400 模型消耗多次 API 请求。
        if not enable_internal_retry:
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
                                allow_google_fallback=allow_google_fallback,
                            )
                        )
                    except Exception as exc:
                        if is_quota_error(exc) or is_sensitive_content_error(exc) or not allow_google_fallback:
                            raise
                        fallback = translate_by_google_with_glossary(sc, glossary or [])
                        results.append(TranslationText(fallback, used_google=True))
                return TranslationText(
                    "\n".join(results),
                    used_google=any(getattr(result, "used_google", False) for result in results),
                )
        if not allow_google_fallback:
            raise
        fallback = translate_by_google_with_glossary(chunk, glossary or [])
        if fallback and fallback != chunk:
            return TranslationText(fallback, used_google=True)
        raise


STREAM_FIRST_TOKEN_TIMEOUT_SEC = 25.0   # 首个 token 必须在 25s 内到达
STREAM_INTER_TOKEN_TIMEOUT_SEC = 20.0   # 相邻两个 token 之间的最大间隔
STREAM_TOTAL_TIMEOUT_SEC = 90.0         # 单次流式翻译的硬上限


def translate_chunk_stream(
    client, chunk, index, total,
    previous_translation="",
    glossary=None,
    model=MODEL_QUALITY,
    on_delta=None,
):
    """Translate a chunk through DashScope's OpenAI-compatible streaming API.

    使用三层超时保护，避免某个模型卡死把整个 SSE 请求拖垮：
    - 首 token 超时：模型 thinking 太久或直接卡住时尽早放弃
    - token 间隔超时：模型中途断流时尽早放弃
    - 整体超时：再慢的模型也不能拖累整个 chunk
    """
    if is_qwen_mt_model(model):
        mt_kwargs = build_qwen_mt_request(chunk, glossary=glossary)
        response = client.chat.completions.create(
            model=model,
            stream=True,
            **mt_kwargs,
        )
    else:
        user_prompt = build_translation_user_prompt(
            chunk, index, total, previous_translation, glossary,
        )
        response = client.chat.completions.create(
            model=model,
            messages=build_chat_messages(SYSTEM_PROMPT, user_prompt, model),
            temperature=0.2,
            stream=True,
        )

    parts = []
    started_at = time.monotonic()
    last_token_at = started_at
    got_first_token = False

    try:
        for event in response:
            now = time.monotonic()

            # 整体超时
            if now - started_at > STREAM_TOTAL_TIMEOUT_SEC:
                raise StreamingTimeoutError(
                    f"stream total timeout >{STREAM_TOTAL_TIMEOUT_SEC}s on {model}"
                )
            # 首 token 超时
            if not got_first_token and now - started_at > STREAM_FIRST_TOKEN_TIMEOUT_SEC:
                raise StreamingTimeoutError(
                    f"first token timeout >{STREAM_FIRST_TOKEN_TIMEOUT_SEC}s on {model}"
                )
            # token 间隔超时
            if got_first_token and now - last_token_at > STREAM_INTER_TOKEN_TIMEOUT_SEC:
                raise StreamingTimeoutError(
                    f"inter-token timeout >{STREAM_INTER_TOKEN_TIMEOUT_SEC}s on {model}"
                )

            choices = getattr(event, "choices", None) or []
            if not choices:
                continue
            delta = getattr(choices[0], "delta", None)
            content = getattr(delta, "content", None) if delta else None
            if not content:
                continue
            parts.append(content)
            got_first_token = True
            last_token_at = now
            if on_delta:
                on_delta(content)
    finally:
        # 主动尝试关闭底层 SSE 连接，避免连接半挂占用资源
        close = getattr(response, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                pass

    return TranslationText("".join(parts).strip())


def translate_by_google_split_with_glossary(chunk: str, glossary: list) -> TranslationText:
    results = [
        translate_by_google_with_glossary(sub_chunk, glossary or [])
        for sub_chunk in split_chunk_further(chunk)
    ]
    return TranslationText("\n".join(results), used_google=True)


def randomized_sensitive_fallback_models():
    """Return a fresh random polling order for sensitive-content fallbacks."""
    return random.sample(SENSITIVE_FALLBACK_MODELS, k=len(SENSITIVE_FALLBACK_MODELS))


SENSITIVE_FALLBACK_MAX_ATTEMPTS = 4


def run_sensitive_model_rotation(callback, max_attempts=SENSITIVE_FALLBACK_MAX_ATTEMPTS, allow_mt=True):
    """Try a few randomly-ordered sensitive-friendly models, then give up.

    - Caps total attempts at ``max_attempts`` so we don't burn through the
      entire pool (15 models × per-model request cost) on hopeless inputs.
    - Exits early on quota errors: those won't be cured by trying more models.
    - allow_mt=False 时跳过 qwen-mt-* 模型（用于 extract_terms 等 mt 无法完成的任务）
    """
    last_exc = None
    full_order = randomized_sensitive_fallback_models()
    if not allow_mt:
        full_order = [m for m in full_order if not is_qwen_mt_model(m)]
    model_order = full_order[:max_attempts]
    for model in model_order:
        try:
            result = callback(model)
            return result, {
                "sensitiveFallback": True,
                "fallback": True,
                "fallbackType": "sensitive_model",
                "modelOrder": model_order,
                "model": model,
            }
        except Exception as exc:
            last_exc = exc
            if is_quota_error(exc):
                break
            continue

    if last_exc:
        raise last_exc
    raise RuntimeError("敏感内容兼容模型池没有可用模型")


def run_sensitive_fallback_models(client, chunk, index, total, previous, glossary):
    return run_sensitive_model_rotation(
        lambda model: translate_chunk(
            client, chunk, index, total, previous,
            glossary=glossary, model=model,
            allow_google_fallback=False,
            enable_internal_retry=False,
        )
    )


# ---------------------------------------------------------------------------
# Korean-residue fixer
# ---------------------------------------------------------------------------

def contains_korean(text: str) -> bool:
    return bool(re.search(r"[\u3131-\u318E\uAC00-\uD7A3]", text))


def fix_korean_line(client, line, previous="", next_line="", model=MODEL_QUALITY):
    if is_qwen_mt_model(model):
        raise RuntimeError(f"fix_korean_line 不支持 Qwen-MT 模型: {model}")
    prompt = (
        "<task>将当前行的韩文残留翻译成中文，其余中文部分原样保留。</task>\n\n"
        f"<previous>{previous}</previous>\n"
        f"<current>{line}</current>\n"
        f"<next>{next_line}</next>"
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
    if is_qwen_mt_model(model):
        raise RuntimeError(f"fix_translation_chunk 不支持 Qwen-MT 模型: {model}")
    glossary_section = build_glossary_prompt_section(glossary, chunk=source_text) if glossary else ""
    fallback_note = (
        "本段经机翻兜底，重点核对人称/称呼/主语。"
        if used_fallback else
        "本段未必经过机翻；无明确错误时保持原样。"
    )
    prompt = (
        f"<task>修正第 {index}/{total} 段。{fallback_note}</task>\n\n"
        f"<glossary>\n{glossary_section}\n</glossary>\n\n"
        f"<previous_translation>\n{previous_translation[-1200:]}\n</previous_translation>\n\n"
        f"<source>\n{source_text}\n</source>\n\n"
        f"<current_translation>\n{translated_text}\n</current_translation>\n\n"
        f"<next_translation>\n{next_translation[:1200]}\n</next_translation>"
    )
    response = client.chat.completions.create(
        model=model,
        messages=build_chat_messages(FIX_SYSTEM_PROMPT, prompt, model),
        temperature=0.2,
    )
    return response.choices[0].message.content.strip()


def fix_fallback_names_and_subjects_chunk(
    client,
    source_text,
    translated_text,
    previous_translation="",
    next_translation="",
    glossary=None,
    index=1,
    total=1,
    model=MODEL_QUALITY,
):
    if is_qwen_mt_model(model):
        raise RuntimeError(f"fix_fallback_names_and_subjects_chunk 不支持 Qwen-MT 模型: {model}")
    glossary_section = build_glossary_prompt_section(glossary, chunk=source_text) if glossary else ""
    prompt = (
        f"<task>窄修正第 {index}/{total} 段：专名/人称/多余主语，其余一律不动。</task>\n\n"
        f"<glossary>\n{glossary_section}\n</glossary>\n\n"
        f"<previous_translation>\n{previous_translation[-800:]}\n</previous_translation>\n\n"
        f"<source>\n{source_text}\n</source>\n\n"
        f"<current_translation>\n{translated_text}\n</current_translation>\n\n"
        f"<next_translation>\n{next_translation[:800]}\n</next_translation>"
    )
    response = client.chat.completions.create(
        model=model,
        messages=build_chat_messages(SIMPLE_FALLBACK_FIX_SYSTEM_PROMPT, prompt, model),
        temperature=0.1,
    )
    return response.choices[0].message.content.strip()


def format_google_fallback_with_source(translated_text: str, source_text: str) -> str:
    translated_text = (translated_text or "").rstrip()
    source_text = (source_text or "").strip()
    if not source_text or "【机器翻译原文】" in translated_text:
        return translated_text
    return f"{translated_text}\n\n【机器翻译原文】\n{source_text}"


def fix_translated_chunks(
    client,
    source_chunks,
    translated_chunks,
    fallback_indices=None,
    google_fallback_indices=None,
    glossary=None,
    model=MODEL_QUALITY,
):
    fallback_set = set(fallback_indices or [])
    google_fallback_set = set(google_fallback_indices or [])
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
        try:
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
        except Exception:
            if not used_fallback:
                raise
            try:
                fixed[idx] = fix_fallback_names_and_subjects_chunk(
                    client,
                    source,
                    translated,
                    previous_translation=previous_translation,
                    next_translation=next_translation,
                    glossary=glossary,
                    index=chunk_no,
                    total=total,
                    model=model,
                )
            except Exception:
                fixed[idx] = translated

        if chunk_no in google_fallback_set:
            fixed[idx] = format_google_fallback_with_source(fixed[idx], source)

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

    def _send_sse_headers(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def _send_sse_event(self, event, data):
        # 关键：data 字段值里如果包含 \n，按 SSE 规范要拆成多行 `data:` 但前端
        # 解析时如果遇到部分接收边界容易出错。改成 ensure_ascii=False + 无缩进，
        # 然后把所有控制换行转义掉，保证一条事件永远只占一行 data。
        payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
        # 把真实换行符替换成转义形式，确保 SSE 一行 data 完整。
        # 前端 JSON.parse 时 \n 会被还原成换行符。
        payload = payload.replace("\r", "").replace("\n", "\\n")
        try:
            self.wfile.write(f"event: {event}\n".encode("utf-8"))
            self.wfile.write(f"data: {payload}\n\n".encode("utf-8"))
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            # 客户端已经关闭连接（关页面、刷新等），不再写
            raise StreamingClientGoneError("client disconnected")

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

    def _ordered_models(self, tier, model_session_id=None, allow_mt=True):
        status = self._current_model_status(tier)
        models = status["models"]
        exhausted = set(status["exhaustedModels"])

        def _filter_mt(seq):
            return seq if allow_mt else [m for m in seq if not is_qwen_mt_model(m)]

        if model_session_id:
            active = [model for model in models if model not in exhausted]
            active = _filter_mt(active)
            if active:
                rng = random.Random(f"{tier}:{model_session_id}")
                rng.shuffle(active)
                return active
            return _filter_mt(models) or models

        start = status["currentIndex"]
        active = [
            models[(start + offset) % len(models)]
            for offset in range(len(models))
            if models[(start + offset) % len(models)] not in exhausted
        ]
        active = _filter_mt(active)
        return active or _filter_mt(models) or models

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

    def _run_with_model_rotation(
        self, tier, callback,
        rotate_on_bad_request=False,
        model_session_id=None,
        allow_mt=True,
    ):
        models = self._ordered_models(tier, model_session_id=model_session_id, allow_mt=allow_mt)
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

    STREAM_INTERRUPTION_MAX_RETRIES = 6

    def _stream_with_model_rotation(
        self,
        tier,
        client,
        chunk,
        index,
        total,
        previous,
        glossary,
        model_session_id=None,
    ):
        models = self._ordered_models(tier, model_session_id=model_session_id)
        first_model = models[0]
        last_exc = None
        interruption_retries = 0  # 中断后切模型重试的累计次数

        for model in models:
            sent_any = False

            def on_delta(delta):
                nonlocal sent_any
                sent_any = True
                self._send_sse_event("delta", {"delta": delta})

            try:
                translated = translate_chunk_stream(
                    client, chunk, index, total, previous,
                    glossary=glossary, model=model, on_delta=on_delta,
                )
                status = self._current_model_status(tier)
                return translated, {
                    "tier": tier,
                    "model": model,
                    "switchedModel": model != first_model,
                    "currentModel": status["model"],
                    "modelOrder": models,
                    "exhaustedModels": status["exhaustedModels"],
                    "interruptionRetries": interruption_retries,
                }
            except StreamingClientGoneError:
                raise
            except Exception as exc:
                last_exc = exc

                if sent_any:
                    if interruption_retries >= self.STREAM_INTERRUPTION_MAX_RETRIES:
                        raise StreamingTranslationInterrupted(str(exc)) from exc

                    interruption_retries += 1
                    try:
                        self._send_sse_event("restart", {
                            "reason": "stream_interrupted",
                            "message": friendly_provider_error(exc),
                            "failedModel": model,
                            "attempt": interruption_retries,
                            "maxAttempts": self.STREAM_INTERRUPTION_MAX_RETRIES,
                            "errorType": type(exc).__name__,
                            "errorDetail": str(exc)[:300],
                        })
                    except StreamingClientGoneError:
                        raise
                    # 配额错误顺便标记耗尽，下次轮换跳过
                    if is_quota_error(exc):
                        self._mark_model_exhausted(tier, model)
                    continue

                # 还没发过 delta 的失败：正常按之前的规则处理
                if is_quota_error(exc):
                    self._mark_model_exhausted(tier, model)
                    continue
                raise

        # 所有模型都试过仍然失败
        if last_exc:
            if interruption_retries > 0:
                raise StreamingTranslationInterrupted(str(last_exc)) from last_exc
            raise last_exc
        raise RuntimeError("没有可用模型")

    def _handle_translate_stream(self, data):
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

        self._send_sse_headers()
        self._send_sse_event("meta", {
            "ok": True,
            "status": "started",
            "index": index,
            "total": total,
            "modelOrder": self._ordered_models(tier, model_session_id=model_session_id),
        })

        try:
            translated, meta = self._stream_with_model_rotation(
                tier, client, chunk, index, total, previous, glossary,
                model_session_id=model_session_id,
            )
            self._send_sse_event("done", {
                "ok": True,
                "translated": str(translated),
                "fallback": False,
                "fallbackType": "",
                **meta,
            })
        except StreamingClientGoneError:
            # 客户端已经断开（关闭页面、刷新等），不再写任何事件。
            return None
        except StreamingTranslationInterrupted as exc:
            # 流式翻译在多次切换模型后仍然中断。先通知前端清空累积，
            # 然后落到敏感兜底 / Google 兜底，让客户端最终拿到一段完整结果。
            self._send_sse_event("restart", {
                "reason": "stream_exhausted",
                "message": friendly_provider_error(exc),
            })
            try:
                translated, meta = run_sensitive_fallback_models(
                    client, chunk, index, total, previous, glossary or [],
                )
                self._send_sse_event("delta", {"delta": str(translated)})
                self._send_sse_event("done", {
                    "ok": True,
                    "translated": str(translated),
                    "note": "此 chunk 已切换兼容模型完成翻译",
                    **meta,
                })
            except Exception:
                translated = translate_by_google_split_with_glossary(chunk, glossary or [])
                self._send_sse_event("delta", {"delta": str(translated)})
                self._send_sse_event("done", {
                    "ok": True,
                    "translated": str(translated),
                    "fallback": True,
                    "fallbackType": "google",
                    "note": "此 chunk 使用了机械翻译",
                    "modelOrder": self._ordered_models(tier, model_session_id=model_session_id),
                })
        except Exception:
            try:
                translated, meta = run_sensitive_fallback_models(
                    client, chunk, index, total, previous, glossary or [],
                )
                self._send_sse_event("delta", {"delta": str(translated)})
                self._send_sse_event("done", {
                    "ok": True,
                    "translated": str(translated),
                    "note": "此 chunk 已切换兼容模型完成翻译",
                    **meta,
                })
            except Exception:
                translated = translate_by_google_split_with_glossary(chunk, glossary or [])
                self._send_sse_event("delta", {"delta": str(translated)})
                self._send_sse_event("done", {
                    "ok": True,
                    "translated": str(translated),
                    "fallback": True,
                    "fallbackType": "google",
                    "note": "此 chunk 使用了机械翻译",
                    "modelOrder": self._ordered_models(tier, model_session_id=model_session_id),
                })
        return None

    def _pick_model(self, data):
        return self._current_model_status(self._tier_name(data))["model"]

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
                    original_text = ensure_accessible_post_content(original_text)
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
                # 术语提取先用轻量模型池：候选词已经在本地按频率和停用词收窄，
                # 让模型只做快速审核/翻译，避免在正式翻译前等待过久。
                # 注意：mt 系列只能翻译、不能筛选/分类，必须跳过。
                try:
                    terms, meta = self._run_with_model_rotation(
                        "light",
                        lambda model: extract_terms(client, text, model=model),
                        model_session_id=model_session_id,
                        allow_mt=False,
                    )
                except Exception:
                    try:
                        terms, meta = run_sensitive_model_rotation(
                            lambda model: extract_terms(client, text, model=model),
                            allow_mt=False,
                        )
                    except Exception:
                        terms, meta = [], {
                            "modelOrder": self._ordered_models("light", model_session_id=model_session_id, allow_mt=False),
                        }
                return self._send_json(200, {"ok": True, "terms": terms, **meta})

            # === MONGODB OPTIONAL WRITES ===
            db_write = build_db_write(action, data)
            if db_write:
                return self._send_db_result(db_write)

            # === TRANSLATE ===
            if action == "translate":
                if data.get("stream"):
                    return self._handle_translate_stream(data)

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
                            allow_google_fallback=False,
                            enable_internal_retry=False,
                        ),
                        model_session_id=model_session_id,
                    )
                    used_google = bool(getattr(translated, "used_google", False))
                    return self._send_json(200, {
                        "ok": True,
                        "translated": str(translated),
                        "fallback": used_google,
                        "fallbackType": "google" if used_google else "",
                        "note": "此 chunk 使用了机械翻译" if used_google else "",
                        **meta,
                    })
                except Exception:
                    try:
                        translated, meta = run_sensitive_fallback_models(
                            client, chunk, index, total, previous, glossary or [],
                        )
                        return self._send_json(200, {
                            "ok": True,
                            "translated": str(translated),
                            "note": "此 chunk 已切换兼容模型完成翻译",
                            **meta,
                        })
                    except Exception:
                        pass

                    translated = translate_by_google_split_with_glossary(chunk, glossary or [])
                    return self._send_json(200, {
                        "ok": True, "translated": str(translated), "fallback": True,
                        "fallbackType": "google",
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
                google_fallback_indices = data.get("google_fallback_indices", [])
                glossary = data.get("glossary", [])
                if not isinstance(fallback_indices, list):
                    fallback_indices = []
                if not isinstance(google_fallback_indices, list):
                    google_fallback_indices = []

                # fix 任务需要 system prompt + 复杂判断，mt 模型不能用
                if isinstance(source_chunks, list) and isinstance(translated_chunks, list) and translated_chunks:
                    fixed, meta = self._run_with_model_rotation(
                        tier,
                        lambda model: fix_translated_chunks(
                            client,
                            [str(chunk) for chunk in source_chunks],
                            [str(chunk) for chunk in translated_chunks],
                            fallback_indices=[int(i) for i in fallback_indices if str(i).isdigit()],
                            google_fallback_indices=[int(i) for i in google_fallback_indices if str(i).isdigit()],
                            glossary=glossary,
                            model=model,
                        ),
                        rotate_on_bad_request=True,
                        model_session_id=model_session_id,
                        allow_mt=False,
                    )
                else:
                    fixed, meta = self._run_with_model_rotation(
                        tier,
                        lambda model: fix_korean_text(client, translated_text, model=model),
                        rotate_on_bad_request=True,
                        model_session_id=model_session_id,
                        allow_mt=False,
                    )
                return self._send_json(200, {"ok": True, "fixed_text": fixed, **meta})

            status, payload = error_response("UNKNOWN_ACTION", 400)
            self._send_json(status, payload)

        except RestrictedPostContentError as e:
            status, payload = error_response("RESTRICTED_POST_CONTENT", 400, str(e))
            return self._send_json(status, payload)
        except Exception as e:
            if is_bad_request_error(e) and not is_quota_error(e):
                status, payload = error_response("PROVIDER_BAD_REQUEST", 400, friendly_provider_error(e))
                return self._send_json(status, payload)
            status, payload = error_response("PROVIDER_UNAVAILABLE", 500, friendly_provider_error(e))
            self._send_json(status, payload)
