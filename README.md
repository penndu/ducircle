# 友链朋友圈 ducircle

面向博客主的公开友链聚合服务。自动发现友链的 RSS、
自动识别博客框架，把好友的更新汇聚成一条时间流。

线上：<https://ducircle.dusays.com>

---

## 它解决什么问题

维护友链时，重复去各个博客看有没有更新很费时间。这个服务：

1. **自动发现** —— 填一个友链页地址或导入 OPML，RSS 自动探测
2. **自动识别框架** —— 识别 Hexo / Hugo / VuePress 等，套用对应规则抓取
3. **失败会明说** —— 抓不到时明确告诉你是哪个站、什么原因，不静默失败
4. **自带 API** —— 一条 URL 就能在你博客里渲染出文章流

---

## 快速开始

### 部署

```bash
git clone git@github.com:penndu/ducircle.git
cd ducircle
cp .env.example .env# 填数据库与 Redis 密码
docker compose -f deploy/docker-compose.yml up -d
```

完整步骤（含 1Panel 环境、反向代理、SSL）见
[docs/部署清单.md](docs/部署清单.md) 和
[docs/GitHub-Actions部署.md](docs/GitHub-Actions部署.md)。

### 本地开发

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
uvicorn app.main:app --reload
```

本地跑测试（用 SQLite，不需要MySQL）：

```bash
pytest tests/ -q
```

---

## 在你的博客里嵌入文章流

拿到 API Key 后（后台「设置」页可重新生成），请求文章流：

```bash
curl "https://ducircle.dusays.com/v1/all?start=0&end=20" \
  -H "X-API-Key: fc_你的密钥"
```

返回：

```json
{
  "data": [
    {
      "title": "文章标题",
      "link": "https://example.com/post",
      "created": "2026-10-23T08:00:00",
      "author": "某个博客",
      "avatar": "https://example.com/avatar.png",
      "summary": "AI 生成的摘要（可能为空）",
      "score": 8.5
    }
  ],
  "total": 128,
  "start": 0,
  "end": 19
}
```

### 友链页自动采集

不用一个个手动加友链。填一个写着别人博客地址的页面地址，
系统自动解析出上面的所有好朋友并加入列表：

```
https://dusays.com/friends/
```

之后每天自动重新检测一次，页面新增了朋友就补进来。

页面上除了友链还有导航、页脚推荐、社交链接、备案号——
这些都会被过滤掉。实测 dusays.com/friends/ 有 59 个站外链接，
准确提取出 50 个真友链，一个都没误伤。

在后台「友链页」页面管理。

### 接入你现有的友链前端

如果你的博客已经在用旧版友链工具，只要换域名和 API Key 就行 ——
字段名沿用了 `title` / `link` / `created` / `author` / `avatar` 的习惯。

### 常用参数

| 参数 | 说明 |
|---|---|
| `start` / `end` | 条数偏移（不是页码），`end=-1` 表示不限 |
| `rule` | 排序：`created`（默认）/ `updated` / `score` |
| `site_id` | 只看某个站点 |
| `only_summarized` | 只看有摘要的 |

其他接口见 `/docs`（服务运行后）。

---

## 抓取是怎么工作的

四层策略，从上到下逐层降级：

```
第0 层  你配的自定义规则      ← 完全信任，不再自动推断
第 1 层  RSS / Atom          ← 最省事也最可靠
第 2 层  框架指纹            ← 识别 Hexo/Hugo 等，用对应选择器
第 3 层  通用推断            ← 按文本密度猜文章容器
```

**实测数据**：4 个真实站点（覆盖 Hexo/Hugo/Ark/anzhiyu 主题），
第 1、2 层能全部命中；feed 不可用时第 2 层仍能救回 4/4。

抓不到时后台会列出失败站点并给出排查建议，你可以给单个站配自定义规则。

---

## 数据与隐私

- 你的数据只有你能看到 —— ORM 层强制按user_id 过滤，
  业务代码写不出越权查询（有 15 个测试专门守这条）
- 数据完全属于你，**一键导出** JSON / CSV / Markdown，也可一键清空
- 删除站点时会连带清除其全部文章，满足站点博主的下架要求
- 抓取遵守目标站点的 robots.txt，可全局关闭

---

## AI 摘要

可选功能。为文章生成中文摘要和 0~10 的推荐分。

两种模式：

- **平台额度** —— 每天有免费额度
- **自带 Key** —— 填自己的 API Key，不消耗平台额度、不受上限限制

正文内容哈希不变时不会重复生成 —— 这一条比任何 prompt 优化都省钱。
手动改过的摘要不会被自动任务覆盖。

支持的模型服务商：SiliconFlow、DeepSeek、Moonshot、Gemini、
本地 Ollama。

---

## 项目结构

```
app/
├── api/          # HTTP 路由（认证、友链、文章、导出、OPML、后台管理）
├── core/         # 配置、安全、会话、限流、租户隔离
├── models/       # SQLAlchemy 模型（9 张表）
├── parser/       # 抓取引擎：custom / feed / fingerprint / heuristic
├── services/     # 业务逻辑：抓取编排、AI 摘要、导出、OPML、调度器
└── web.py        # Jinja2 模板环境
web/templates/    # 后台页面（Tailwind，中文，响应式）
tests/            # 277 个测试
scripts/          # 验证与维护脚本
docs/             # 需求、架构、部署、抓取验证报告
```

---

## 验证状态

诚实标注哪些经过实测、哪些没有：

| 项 | 状态 | 依据 |
|---|---|---|
| 抓取引擎 |✅ | 真实站点 4/4（akilar.top、elizen.me 等） |
| 框架指纹兜底 | ✅ | feed 不可用时仍能 4/4 |
| 数据库迁移 | ✅ | 真实 MySQL 8.0.40 上执行成功 |
| 租户隔离 | ✅ | 13 项真实 MySQL 行为检查 |
| OPML 导入 | ✅ | 5 种阅读器导出格式 |
| 正文抽取 | ✅ | 真实文章页面验证 |
| 单元测试 | ✅ | 277 个用例 |
| Redis | ⚠️ | 未在真实环境验证（开发机无 Redis） |
| 端到端部署 | ⚠️ | 首次部署后按文档逐项确认 |

`docs/抓取能力验证报告.md` 里有详细的实测数据和踩过的坑。

---

## 开发约定

改代码前先看这几条，都是被真实 bug 教出来的：

1. **改解析规则后必须跑** `pytest tests/` + `scripts/live_check.py`
2. **数据库相关的代码 SQLite 测过不等于 MySQL 能跑** ——
   索引参数、外键指向、collation 行为 SQLite 全都不校验。
   用 `scripts/verify_mysql.sh` 在真实 MySQL 上验。
3. **`await db.execute(q).scalar_one()` 是错的** ——
   Python 解析成 `await (db.execute(q).scalar_one())`，
   方法会作用在协程上。正确写法是 `(await db.execute(q)).scalar_one()`。
   `scripts/check_await_usage.py` 会在 CI 里检查这个。
4. **缺一个 import 会让整条路由的依赖注入静默失效** ——
   `get_type_hints` 抛 NameError，所有 `Depends` 都不执行。
   `tests/test_routes.py` 会检查。

---

## 许可

MIT