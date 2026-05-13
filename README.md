# Postype Translator

一个用于将 Postype 韩文小说正文翻译为简体中文的网页工具。

前端为单页 HTML，无前端框架；后端为 Python serverless API。翻译模型使用阿里云 DashScope 国际版的 OpenAI 兼容接口。

## 功能

### 输入方式

支持三种输入方式：

- 输入 Postype 页面 URL
- 手动粘贴韩文正文
- 上传 `.html` 或 `.webarchive` 文件

### 翻译流程

工具会先解析正文并切分为多个 chunk，然后在翻译前提取篇章术语。用户可以在“篇章术语确认”面板中检查、修改、删除或新增术语，再开始翻译。

当前流程包括：

1. 解析 URL、手动文本或上传文件
2. 提取 Postype 正文
3. 按 chunk 切分正文
4. 使用模型提取篇章术语
5. 用户确认术语表
6. 逐段翻译正文
7. 检测译文中的韩文残留并修正

### 术语库

工具内置全局术语库，保存在浏览器 `localStorage` 中。

全局术语库支持：

- 添加术语
- 编辑术语
- 删除术语
- 导入 JSON
- 导出 JSON
- 恢复默认术语库

翻译时，全局术语库优先级高于篇章自动提取的术语。篇章术语只会补充全局术语库中不存在的词条。

默认术语库包含部分 TWS 相关艺名、本名和常用称呼。

### 篇章术语确认

每次翻译前，后端会调用模型通读正文样本并提取术语，包括：

- 人名
- 地名
- 技能名
- 称号
- 物品名
- 组织名
- 称呼
- 其他专有名词

用户确认后，最终术语表会注入每个翻译 prompt，用于保持人名、称呼和专有名词一致。

### 快速模式

页面提供“快速模式”开关。

- 普通模式使用质量模型
- 快速模式使用快速模型，并在前端以批量并发方式翻译 chunk

当前前端并发批量大小为 `4`。

### 机翻 fallback

当 Qwen 翻译失败时，后端会尝试使用 Google Translate 接口作为 fallback，并对 fallback 译文进行术语表字符串替换。

## 技术栈

### 前端

- HTML
- CSS
- 原生 JavaScript
- `localStorage`

### 后端

- Python
- `BaseHTTPRequestHandler`
- OpenAI Python SDK
- BeautifulSoup
- lxml
- requests

### 翻译 API

- 阿里云 DashScope 国际版
- OpenAI compatible mode
- Qwen 模型

当前后端模型配置位于 `api/app.py`：

```python
MODEL_QUALITY = "qwen-max"
MODEL_FAST    = "qwen2.5-vl-72b-instruct"
MAX_CHARS     = 3000
```

## 项目结构

```text
.
├── api/
│   └── app.py
├── public/
│   └── index.html
├── requirements.txt
└── README.md
```

## 环境变量

后端需要配置 DashScope API Key：

```text
DASHSCOPE_API_KEY=your_dashscope_api_key
```

如果没有配置该环境变量，后端会返回：

```text
服务器未配置 DASHSCOPE_API_KEY
```

## API

前端通过 `POST /api/translate` 调用后端。

后端根据请求体中的 `action` 字段执行不同操作。

### prepare

解析输入内容并切分正文。

请求示例：

```json
{
  "action": "prepare",
  "url": "https://..."
}
```

或：

```json
{
  "action": "prepare",
  "text": "韩文正文"
}
```

返回示例：

```json
{
  "ok": true,
  "chunks": ["..."],
  "total": 3
}
```

### extract_terms

从正文中提取篇章术语。

请求示例：

```json
{
  "action": "extract_terms",
  "text": "韩文正文"
}
```

返回示例：

```json
{
  "ok": true,
  "terms": [
    {
      "ko": "신유",
      "zh": "申惟",
      "category": "人名"
    }
  ]
}
```

### translate

翻译单个 chunk。

请求示例：

```json
{
  "action": "translate",
  "chunk": "韩文正文片段",
  "index": 1,
  "total": 3,
  "previous": "上一段译文",
  "glossary": [
    {
      "ko": "신유",
      "zh": "申惟",
      "category": "艺名"
    }
  ],
  "fast": false
}
```

返回示例：

```json
{
  "ok": true,
  "translated": "译文",
  "fallback": false
}
```

### fix

修正译文中的韩文残留。

请求示例：

```json
{
  "action": "fix",
  "translated_text": "已经翻译但仍有韩文残留的文本",
  "fast": false
}
```

返回示例：

```json
{
  "ok": true,
  "fixed_text": "修正后的文本"
}
```

## 部署

项目可部署到 Vercel。

部署时需要：

1. 安装 Python 依赖：

```text
openai
requests
beautifulsoup4
lxml
```

2. 配置环境变量：

```text
DASHSCOPE_API_KEY
```

3. 确认前端 API 路径 `/api/translate` 能正确路由到后端 handler。

## 使用说明

1. 打开网页
2. 选择输入方式：URL、手动输入或文件上传
3. 根据需要编辑全局术语库
4. 点击“翻译”
5. 在“篇章术语确认”面板中检查术语
6. 点击“确认并翻译”
7. 在输出框查看译文
8. 点击“导出 TXT”保存译文

## 术语 JSON 格式

导入和导出的术语库 JSON 为数组格式：

```json
[
  {
    "ko": "신유",
    "zh": "申惟",
    "category": "艺名"
  },
  {
    "ko": "도훈",
    "zh": "道勋",
    "category": "艺名"
  }
]
```

字段说明：

- `ko`：韩文原文
- `zh`：中文译名
- `category`：术语类别

## 注意事项

- URL 抓取依赖页面 HTML 中的 `id="post-content"` 正文区域。
- `.webarchive` 文件会解析主资源中的 HTML 内容。
- 浏览器本地术语库存储在 `localStorage` 中，清除浏览器数据可能会删除术语库。
- Google Translate fallback 依赖外部网络访问。
- 翻译质量和速度会受到模型、正文长度、API 状态和网络环境影响。
