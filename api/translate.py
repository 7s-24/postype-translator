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


def fetch_postype_text(url: str) -> str:
    headers = {
        "User-Agent": "Mozilla/5.0 Chrome/122.0 Safari/537.36"
    }
    resp = requests.get(url, headers=headers, timeout=30)
    resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "lxml")
    content = soup.find(id="post-content")

    if content is None:
        raise RuntimeError("没有找到 id='post-content'，可能需要登录、付费或 JS 渲染。")

    for tag in content.find_all(["script", "style", "button", "nav", "aside"]):
        tag.decompose()

    text = content.get_text("\n", strip=True)
    text = re.sub(r"\n{3,}", "\n\n", text)

    if not text.strip():
        raise RuntimeError("正文为空。")

    return text.strip()


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


class handler(BaseHTTPRequestHandler):
    def _send_json(self, status, data):
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False).encode("utf-8"))

    def do_OPTIONS(self):
        self._send_json(200, {"ok": True})

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
            data = json.loads(body.decode("utf-8"))

            url = data.get("url", "").strip()
            if not url:
                self._send_json(400, {"error": "缺少 URL"})
                return

            api_key = os.getenv("DASHSCOPE_API_KEY")
            if not api_key:
                self._send_json(500, {"error": "服务器未配置 DASHSCOPE_API_KEY"})
                return

            client = OpenAI(
                base_url="https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
                api_key=api_key,
            )

            original_text = fetch_postype_text(url)
            chunks = split_text(original_text)

            translated_parts = []
            for i, chunk in enumerate(chunks, start=1):
                previous = translated_parts[-1] if translated_parts else ""
                translated = translate_chunk(client, chunk, i, len(chunks), previous)
                translated_parts.append(translated)
                time.sleep(0.5)

            self._send_json(200, {
                "ok": True,
                "chunks": len(chunks),
                "original": original_text,
                "translated": "\n\n".join(translated_parts),
            })

        except Exception as e:
            self._send_json(500, {"error": str(e)})