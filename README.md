# 我真的想看

一个用于将 Postype 韩文小说正文翻译为简体中文的网页工具。
是朕执意要看的要罚就罚朕吧！
付费和成人内容不支持URL输入，需要手动复制黏贴文本，或者导入文件哦。

## 功能

### 输入方式

支持三种输入方式：

- 输入 Postype 页面 URL
- 手动粘贴韩文正文
- 上传 `.html` 或 `.webarchive` 文件

### 翻译流程

工具会先解析正文并切分为多个 chunk，然后在翻译前提取篇章术语。用户可以在“篇章术语确认”面板中检查、修改、删除或新增术语，再开始翻译。

### 快速模式

页面提供“快速模式”开关。

- 普通模式使用质量模型
- 快速模式使用快速模型，并在前端以批量并发方式翻译

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

### 翻译模型

- Qwen

## 注意事项

- URL 抓取依赖页面 HTML 中的 `id="post-content"` 正文区域。
- `.webarchive` 文件会解析主资源中的 HTML 内容。但因为`.webarchive` 文件一般太大，所以基本是摆设状态。
- 浏览器有本地术语库，清除浏览器数据可能会删除术语库。
- Google Translate fallback 依赖外部网络访问。正在修改成更适合大陆的连接方式。
- 翻译质量和速度会受到模型、正文长度、API 状态和网络环境影响。如果翻译不理想，建议朴素地重试一次。
