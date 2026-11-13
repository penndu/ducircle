# 更新日志

本文件记录面向用户的重要变更。格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

---

## [1.1.0] - 2026-11-13

### 新增

- **设置页可修改密码** —— 账号区新增改密表单（原密码 + 新密码 + 确认）。
  - **必须验证原密码**：只凭已登录就能改密的话，
    别人拿到你的 Cookie（比如 XSS 拿到）就能永久夺走账号。
  - 新密码长度要求与注册一致（8~128 位）。
  - 改密后**其它设备的登录状态立即失效**，当前设备不受影响
    （响应里换发新 Cookie）。

### 内部

- Session 支持指定签发时间，改密后凭签发时间作废旧会话。
- 新增 `users.password_changed_at`（Unix 时间戳）。

---

## [1.0.1] - 2026-11-13

### 修复

- **概览页「最新文章」摘要出框** —— 宽屏（≥1400px）下概览页是双列布局，
  每列只有 650~750px；摘要用单行省略（`nowrap` + `text-overflow:ellipsis`），
  64 个中文字（12px 字号）需要约 770px，放不下就横向溢出、压到相邻内容上。
  改为两行截断（`line-clamp`），文字正常换行，任何视口宽度都不会出框。

---

## [1.0.0] - 2026-11-13

首个正式版本。线上：<https://ducircle.dusays.com>

### 新增

**公开只读 API**

- `GET /v1/all` —— 文章流（分页）
- `GET /v1/friends` —— 友链列表
- `GET /v1/stats` —— 统计概览
- 支持跨域访问，白名单由 `CORS_ORIGINS` 配置，**只对 `/v1/*` 开放**
  （后台接口靠 Cookie 会话鉴权，开放跨域等于把会话暴露给任意站点）
- 四套前台调用代码：原生 JS / React / Vue 3 / Hexo 页面模板，
  可在 `/docs` 页复制下载（`/static/ducircle.js` 等）

**AI 摘要**

- 基于用户自己的 API Key（支持 MiniMax、DeepSeek、硅基流动、Gemini 等）
- 文章摘要 + 推荐分（0~10）+ 打分理由
- 批量生成带实时进度反馈：跑了几篇、失败没、为什么失败
- 失败原因可见：不再「点了没反应」

**采集稳定性**

- 四层解析策略：自定义规则 → RSS/Atom → 框架指纹 → 通用推断
- 自动发现 RSS、自动识别 Hexo/Hugo/VuePress 等框架
- 失败会明说：哪个站、什么原因，不静默失败
- 连续失败 10 次自动**暂停采集**，需手动恢复
- 同一篇文章按 RSS guid 去重（永久链接变更、换域名都不会重复入库）

**其它**

- 设置页可配置采集频率（cron）
- 友链页自动采集 + 定期检测新友链
- 友链管理支持编辑网址与 RSS 地址
- OPML 导入
- 公开落地页、后台概览、API 文档、状态页

### 修复

本次开发期间修复的关键问题：

- **定时抓取从未真正执行过** —— 后台任务静默失败
- **批量摘要一篇都没生成** —— `limit` 被配额上限覆盖，接口却返回 200
- **同一篇文章被采 3 次** —— 永久链接从 `/2026/10/2281/` 变为 `/post/2281/`、域名 `.com` 换 `.cn`
- **90% 文章抓不到正文** —— SPA 站点文章页只返回骨架页，改用 RSS feed 里的全文
- **模型返回不合法 JSON 时，JSON 原文被存进数据库** —— 页面上直接显示 `{"summary":"...`
- **永久失败的文章被反复重试** —— 内容安全拦截类失败重试多少次都一样

### 已知限制

- 约 9% 的文章无法生成摘要：站点文章页是 SPA/JS 防护页且文章已滑出 feed 窗口，
  或模型内容安全拦截。这类文章需要无头浏览器才能抓取，
  考虑到服务器资源限制暂未引入。
- 平台不提供免费摘要额度，需自行填写 API Key。

### 技术栈

Python 3.12 · FastAPI · SQLAlchemy 2.0 (async) · MySQL 8 · Redis ·
Alembic · trafilatura · feedparser · httpx · Docker

---

[1.1.0]: https://github.com/penndu/ducircle/releases/tag/v1.1.0
[1.0.1]: https://github.com/penndu/ducircle/releases/tag/v1.0.1
[1.0.0]: https://github.com/penndu/ducircle/releases/tag/v1.0.0
