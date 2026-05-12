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

# ---------------------------------------------------------------------------
# Models & config
# ---------------------------------------------------------------------------
MODEL_QUALITY = "qwen-max"
MODEL_FAST    = "qwen2.5-vl-72b-instruct"
MAX_CHARS     = 3000          # bigger chunks → fewer API calls

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
- 成人内容按原文强度翻译，不额外加重，也不弱化。
- 不确定说话人时，不要擅自添加主语。
- 原文故意省略主语时，中文也可以适度省略。
"""

EXTRACT_TERMS_PROMPT = """你是专业韩文小说术语提取器。请阅读以下韩文小说文本，提取所有专有名词和术语。

重点关注：
1. 带 「」『』【】〈〉《》"" '' 及其他引号或括号包裹的词（通常是技能名、称号、物品名、作品名等）
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
            messages=[
                {"role": "system", "content": EXTRACT_TERMS_PROMPT},
                {"role": "user", "content": sampled},
            ],
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
    except Exception:
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
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.2,
        )
        return response.choices[0].message.content.strip()
    except Exception:
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
                    except Exception:
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
        messages=[
            {"role": "system", "content": FIX_SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
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

    def _pick_model(self, data):
        return MODEL_FAST if data.get("fast") else MODEL_QUALITY

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
            data = json.loads(body.decode("utf-8"))
            action = data.get("action", "")

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
                # Always use quality model for term extraction (runs once)
                terms = extract_terms(client, text, model=MODEL_QUALITY)
                return self._send_json(200, {"ok": True, "terms": terms})

            # === TRANSLATE ===
            if action == "translate":
                chunk = data.get("chunk", "")
                index = int(data.get("index", 1))
                total = int(data.get("total", 1))
                previous = data.get("previous", "")
                glossary = data.get("glossary", [])
                model = self._pick_model(data)

                if not chunk:
                    return self._send_json(400, {"error": "缺少 chunk"})
                client = self._get_client()
                if not client:
                    return self._send_json(500, {"error": "服务器未配置 DASHSCOPE_API_KEY"})

                try:
                    translated = translate_chunk(
                        client, chunk, index, total, previous,
                        glossary=glossary, model=model,
                    )
                    return self._send_json(200, {
                        "ok": True, "translated": translated, "fallback": False,
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
                model = self._pick_model(data)
                fixed = fix_korean_text(client, translated_text, model=model)
                return self._send_json(200, {"ok": True, "fixed_text": fixed})

            self._send_json(400, {"error": "未知 action"})

        except Exception as e:
            self._send_json(500, {"error": str(e)})
