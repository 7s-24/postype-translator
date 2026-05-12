from http.server import BaseHTTPRequestHandler
from openai import OpenAI
from bs4 import BeautifulSoup
import requests
import json
import os
import re
import time

MODEL = "qwen-plus-2025-07-28"
MAX_CHARS = 3000

SYSTEM_PROMPT = """你是专业韩文同人小说翻译器，负责将韩文正文翻译成简体中文。

【核心要求】
- 忠实翻译原文，不删减、不总结、不规避、不弱化。
- 保留原文的情绪、暧昧张力、人物关系、语气词、停顿、喘息、拟声词。
- 不添加原文没有的信息。
- 不解释，不加注释，不输出前缀。
- 只输出译文。

【TWS 成员名词表】
艺名：
- 신유 → 申惟
- 도훈 → 道勋
- 영재 → 英宰
- 한진 → 韩振
- 지훈 → 志薰
- 경민 → 炅潣

本名：
- 신정환 → 申正焕
- 김도훈 → 金道勋
- 최영재 → 崔英宰
- 한진 → 韩振
- 한지훈 → 韩志薰
- 이경민 → 李炅潣

【人名处理规则】
- 原文使用艺名时，译文使用对应艺名。
- 原文使用本名时，译文使用对应本名。
- 不要把本名自动改成艺名。
- 不要把艺名自动扩写成本名。
- 如果原文只写名字，不要补姓。
- 同一个人在同一称呼体系下必须前后一致。

【称呼处理】
- 형 → 哥
- 선배 → 前辈
- 후배 → 后辈
- 막내 → 忙内 / 老幺，按语境选择
- 이름 + 아/야 是亲昵称呼，按中文语感处理。

【文风要求】
- 成人内容按原文强度翻译，不额外加重，也不弱化。
- 不确定说话人时，不要擅自添加主语。
- 原文故意省略主语时，中文也可以适度省略。
"""

FIX_SYSTEM_PROMPT = """你是专业韩文同人小说翻译器，负责修正已经翻译文本中的韩文残留。

【修正要求】
- 只翻译文本中的韩文部分，保留其余已经是中文的内容原样。
- 不要解释、不加注释、不输出前缀。
- 不要改写本已是中文的部分。
- 尽量参考上下文保持说话风格一致。
- 只返回修正后的结果。
"""


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
    headers = {
        "User-Agent": "Mozilla/5.0 Chrome/122.0 Safari/537.36"
    }
    resp = requests.get(url, headers=headers, timeout=30)
    resp.raise_for_status()

    return parse_postype_html(resp.text, section_id="post-content")


def split_text(text: str, max_chars: int = MAX_CHARS):
    paragraphs = text.replace("\r\n", "\n").split("\n")
    chunks = []
    current = ""

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
                    chunks.append(para[i:i + max_chars])
                current = ""
            else:
                current = para + "\n\n"

    if current.strip():
        chunks.append(current.strip())

    return chunks


def translate_chunk(client, chunk, index, total, previous_translation=""):
    context = ""
    if previous_translation:
        context = f"""【上一段译文结尾，仅用于保持上下文一致，不要重复翻译】
{previous_translation[-800:]}

"""

    user_prompt = f"""{context}下面是韩文小说正文的第 {index}/{total} 段。

请直接翻译成简体中文。注意承接上一段的人物称呼、语气、情绪和文风，但不要重复上一段内容。

【当前原文】
{chunk}
"""

    response = client.chat.completions.create(
        model=MODEL,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        temperature=0.2,
    )

    return response.choices[0].message.content.strip()


def contains_korean(text: str) -> bool:
    return bool(re.search(r"[\u3131-\u318E\uAC00-\uD7A3]", text))


def fix_korean_line(client, line: str, previous: str = "", next_line: str = "") -> str:
    prompt = f"""下面是一段已经翻译成中文的文本，其中仍有韩文残留。
请只翻译文本中的韩文部分为简体中文，保留其他已经是中文的内容原样。
不要添加注释、说明、前缀或额外内容。不要改写已是中文的部分。

上一行（仅供参考）：{previous}
当前行：{line}
下一行（仅供参考）：{next_line}
"""

    response = client.chat.completions.create(
        model=MODEL,
        messages=[
            {"role": "system", "content": FIX_SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        temperature=0.2,
    )

    return response.choices[0].message.content.strip()


def fix_korean_text(client, text: str) -> str:
    lines = text.splitlines()
    fixed_lines = []

    for idx, line in enumerate(lines):
        if contains_korean(line):
            previous = lines[idx - 1] if idx > 0 else ""
            next_line = lines[idx + 1] if idx < len(lines) - 1 else ""
            fixed_lines.append(fix_korean_line(client, line, previous, next_line))
        else:
            fixed_lines.append(line)

    return "\n".join(fixed_lines)


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
        self._send_json(200, {
            "ok": True,
            "message": "Postype translator API is running. Use POST /api/translate."
        })
    
    def do_OPTIONS(self):
        self._send_json(200, {"ok": True})
        
    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
            data = json.loads(body.decode("utf-8"))
            action = data.get("action", "")

            if action == "prepare":
                html = data.get("html", "")
                if html:
                    original_text = parse_postype_html(html, section_id="post-content")
                else:
                    url = data.get("url", "").strip()
                    if not url:
                        self._send_json(400, {"error": "缺少 URL 或 HTML 文件内容"})
                        return
                    original_text = fetch_postype_text(url)

                chunks = split_text(original_text)
                self._send_json(200, {
                    "ok": True,
                    "chunks": chunks,
                    "total": len(chunks),
                })
                return

            if action == "translate":
                chunk = data.get("chunk", "")
                index = int(data.get("index", 1))
                total = int(data.get("total", 1))
                previous = data.get("previous", "")

                if not chunk:
                    self._send_json(400, {"error": "缺少 chunk"})
                    return

                api_key = os.getenv("DASHSCOPE_API_KEY")
                if not api_key:
                    self._send_json(500, {"error": "服务器未配置 DASHSCOPE_API_KEY"})
                    return

                client = OpenAI(
                    base_url="https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
                    api_key=api_key,
                )

                translated = translate_chunk(client, chunk, index, total, previous)
                self._send_json(200, {"ok": True, "translated": translated})
                return

            if action == "fix":
                translated_text = data.get("translated_text", "")
                if not translated_text:
                    self._send_json(400, {"error": "缺少 translated_text"})
                    return

                api_key = os.getenv("DASHSCOPE_API_KEY")
                if not api_key:
                    self._send_json(500, {"error": "服务器未配置 DASHSCOPE_API_KEY"})
                    return

                client = OpenAI(
                    base_url="https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
                    api_key=api_key,
                )

                fixed_text = fix_korean_text(client, translated_text)
                self._send_json(200, {"ok": True, "fixed_text": fixed_text})
                return

            self._send_json(400, {"error": "未知 action，应为 prepare、translate 或 fix"})

        except Exception as e:
            self._send_json(500, {"error": str(e)})
