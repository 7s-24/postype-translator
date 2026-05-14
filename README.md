# 我真的想看

一个用于将 Postype 韩文小说正文翻译为简体中文的网页工具。

是朕执意要看的，要罚就罚朕吧！

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

- 普通模式使用标准模型池，优先保证翻译质量
- 快速模式使用轻量模型池，优先保证速度和并发
- 每次开始翻译前会确认当前模式使用到哪个模型；如果检测到额度耗尽，会自动切换到同一模型池里的下一个模型
- 模型池分类和轮换细节见 [`docs/model-pools.md`](docs/model-pools.md)

## 技术栈

### 前端

- HTML
- CSS
- JavaScript
- `localStorage`

### 后端

- Python

### 翻译模型

- Qwen

## 注意事项

- URL 抓取依赖页面 HTML 中的 `id="post-content"` 正文区域。
- `.webarchive` 文件会解析主资源中的 HTML 内容。但因为`.webarchive` 文件一般太大，所以基本是摆设状态。
- 浏览器有本地术语库，清除浏览器数据可能会删除术语库。
- Google Translate fallback 依赖外部网络访问。正在修改成更适合大陆的连接方式。
- 翻译质量和速度会受到模型、正文长度、API 状态和网络环境影响。如果翻译不理想，建议朴素地重试一次。

## MongoDB 持久化（可选）

当前前端的浏览器 `localStorage` 术语库能力保持不变；MongoDB 仅作为后端可选的数据访问层，用于后续把需要跨设备、可审核或可统计的数据保存到服务端。

### 环境变量

部署环境需要配置以下变量：

- `MONGODB_URI`：MongoDB Atlas 或其他 MongoDB 实例连接字符串。
- `MONGODB_DB_NAME`：应用使用的数据库名称，例如 `postype_translator`。

如果没有配置这两个变量，原有翻译和本地 `localStorage` 功能仍可使用；只有调用 MongoDB 写入相关接口时会返回“MongoDB 未配置”。

### 建议集合

建议在 MongoDB Atlas 中创建以下集合：

- `site_likes`：网页点赞统计。
- `glossary_uploads`：用户上传的术语库草稿。
- `glossary_entries`：审核通过后可复用的公共术语条目。
- `events`：轻量运营事件，例如翻译开始、翻译完成、术语导入、点赞。

### 本地开发

1. 安装依赖：

   ```bash
   pip install -r requirements.txt
   ```

2. 配置本地环境变量：

   ```bash
   export MONGODB_URI="mongodb+srv://<user>:<password>@<cluster>/<options>"
   export MONGODB_DB_NAME="postype_translator"
   ```

3. 如果只是调试翻译或前端本地术语库，可以不配置 MongoDB；后端会延迟初始化数据库连接，不会影响既有 `localStorage` 流程。

### 数据写入约束

后端 MongoDB 写入只保存白名单字段，不会直接把完整请求体写入数据库。当前预留的写入动作包括：

- `record_like`：写入或更新 `site_likes`。
- `save_glossary_upload`：写入 `glossary_uploads` 草稿。
- `save_glossary_entries`：写入审核通过的 `glossary_entries`。
- `track_event`：写入 `events` 运营事件。
