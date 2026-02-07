# Search Stack

AI Agent 专用的 Web 搜索与抓取中间层。

为 [OpenClaw](https://openclaw.com)、Claude Code、Dify 等 AI 智能体提供统一的 Web 访问 API：多引擎搜索自动 fallback、无头 Chrome 反爬渲染、Cookie 注入登录态、正文精准提取。一次部署，所有 Agent 共用。

## 为什么需要 Search Stack

| 痛点 | Search Stack 的解决方案 |
|------|------------------------|
| Brave/Google 搜索有免费额度限制 | 多引擎 fallback（Tavily → Serper → SearXNG），SearXNG 完全免费无限量 |
| AI 抓取网页被 Cloudflare/反爬挡住 | 内置 Browserless 无头 Chrome，启用 Stealth 模式绕过检测 |
| 需要登录的网站（知乎、小红书等）抓不到正文 | Cookie 管理 API + 自动注入渲染 + 登录检测引导 |
| Agent 被诱导访问内网（SSRF） | 内置私网 IP 黑名单 + DNS 校验 |
| 搜索结果只有摘要，没有全文 | `enrich=true` 搜索后自动抓取每条结果全文 |

### 与 Brave Search 对比

> 以下数据基于实际测试（2026-02-07），搜索关键词："claude opus 4.6 评测"

| 维度 | Search Stack | Brave Search（OpenClaw 内置） |
|------|-------------|-------------------------------|
| **搜索速度** | 0.8-1.5s（Tavily/Serper）| ~1-2s |
| **缓存命中** | **13ms**（Redis 缓存 15 分钟）| 无缓存，每次重新请求 |
| **中文搜索** | 结果丰富（掘金、知乎、什么值得买等）| 中文结果偏少，偏英文源 |
| **英文搜索** | 优秀 | 优秀 |
| **高可用** | 三引擎自动 fallback | 单点，挂了就没了 |
| **全文抓取** | `enrich=true` 搜索+正文一步到位 | 只返回摘要，需额外抓取 |
| **反爬站点** | Browserless Chrome 渲染 | 无法抓取 |
| **需登录站点** | Cookie 注入 + 自动检测引导 | 不支持 |
| **免费额度** | SearXNG 无限量兜底 | 免费 Key 有严格限制 |

**结论：搜索速度持平，中文质量更好，功能远超 Brave。**

## 架构

```
                         +-----------+
    AI Agent  ──────────>| search-   |──> Tavily API
    (OpenClaw / Claude)  | proxy     |──> Serper API (Google)
         POST /search    | (FastAPI) |──> SearXNG (self-hosted)
         POST /fetch     +-----+-----+
                               |
                 +-------------+-------------+
                 |                           |
           +-----+-----+           +--------+--------+
           |   Redis    |           |   Browserless   |
           | (cache +   |           | (headless Chrome |
           |  rate-limit)|          |  anti-bot render)|
           +------------+           +-----------------+
```

**四个容器，一键启动：**

| 服务 | 作用 |
|------|------|
| **search-proxy** | FastAPI 核心代理，统一搜索/抓取接口 |
| **Redis** | 结果缓存（15 分钟 TTL）+ API 限流计数 |
| **SearXNG** | 自托管元搜索引擎（聚合 Google、DuckDuckGo、Brave 等，免费无限量） |
| **Browserless** | 无头 Chrome，渲染 JS 页面，Stealth 模式绕过反爬 |

## 特性

- **多引擎 Fallback** — Tavily → Serper → SearXNG 按优先级自动切换，单引擎挂不影响服务
- **搜索 + 抓取一体** — `/search` 搜索，`/fetch` 抓取正文，`enrich=true` 搜索后自动抓取全文
- **抗反爬** — Browserless Stealth 模式，绕过 Cloudflare / JS Challenge
- **正文提取** — trafilatura + BeautifulSoup + readability 三引擎，精准提取正文
- **Cookie 管理** — API 动态增删 Cookie，自动注入 Chrome 渲染，支持直接粘贴浏览器 Cookie
- **登录检测** — 自动检测"需要登录"页面，返回 `needs_login` 标记
- **SSRF 防护** — 拒绝访问私网 IP（127/10/172.16/192.168/169.254）
- **URL 去重** — 自动去除追踪参数（utm_*、fbclid 等），同域名结果限制
- **Redis 缓存** — 15 分钟 TTL，重复查询即时返回
- **API Key 鉴权 + 限流** — 滑动窗口限流
- **MCP Server** — stdio 模式 MCP Server（`mcp-server.ts`），可通过 mcporter 注册供 OpenClaw 等 Agent 使用
- **TikHub 社交媒体 API** — 可选集成，代理 TikHub 803 个社交平台工具（抖音、TikTok、微博等），内置自动回退
- **全异步** — async Redis + 共享 httpx 连接池，高并发低延迟

---

## 快速部署

### 前置要求

- Docker + Docker Compose
- （可选）[Tavily](https://tavily.com) API Key — 免费 1000 次/月
- （可选）[Serper](https://serper.dev) API Key — 免费 2500 次

> 不配 Tavily / Serper 也能用，会自动 fallback 到 SearXNG（完全免费）。

### 1. 克隆项目

```bash
git clone https://github.com/pinkpills/search-stack.git
cd search-stack
```

### 2. 配置环境变量

```bash
cp .env.example .env
```

编辑 `.env`：

```bash
# ====== 搜索引擎 API Key（可选）======
TAVILY_API_KEY=your_tavily_key
SERPER_API_KEY=your_serper_key

# ====== 内部服务密钥（必填，每个值必须不同）======
SEARXNG_SECRET=
PROXY_API_KEY=
BROWSERLESS_TOKEN=
REDIS_PASSWORD=
```

一键生成随机密钥：

```bash
python3 -c "
import secrets
for name in ['SEARXNG_SECRET', 'PROXY_API_KEY', 'BROWSERLESS_TOKEN', 'REDIS_PASSWORD']:
    print(f'{name}={secrets.token_hex(16)}')
" >> .env
```

### 3. 配置 SearXNG

> **必做！** 不做这步 SearXNG 的 JSON API 会返回 403。

```bash
cp searxng/settings.yml.example searxng/settings.yml
```

编辑 `searxng/settings.yml`，确保包含：

```yaml
search:
  formats:
    - html
    - json     # ← 必须有这行，否则 JSON API 返回 403
```

如果你之前已经启动过 SearXNG（它会自动生成 `settings.yml`），需要手动加上 `formats` 配置后重启容器。

### 4. 启动服务

```bash
docker compose -f search-stack.yml up -d
```

等待所有容器健康（约 30 秒）：

```bash
docker compose -f search-stack.yml ps
```

全部显示 `healthy` 即完成。

### 5. 验证

```bash
# 健康检查
curl -s -H "X-API-Key: YOUR_PROXY_API_KEY" http://127.0.0.1:17080/health | python3 -m json.tool

# 搜索测试（自动选择引擎）
curl -s -X POST http://127.0.0.1:17080/search \
  -H "X-API-Key: YOUR_PROXY_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"query": "hello world", "count": 3}' | python3 -m json.tool

# 指定 SearXNG 搜索（验证 SearXNG 是否正常）
curl -s -X POST http://127.0.0.1:17080/search \
  -H "X-API-Key: YOUR_PROXY_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"query": "hello world", "count": 3, "provider": "searxng"}' | python3 -m json.tool

# 抓取测试
curl -s -X POST http://127.0.0.1:17080/fetch \
  -H "X-API-Key: YOUR_PROXY_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"url": "https://example.com", "render": false}' | python3 -m json.tool
```

> **提示：** 如果 SearXNG 返回 502 或空结果，大概率是缺少 `formats: [html, json]` 配置，参见步骤 3。

---

## 集成 OpenClaw

Search Stack 可以作为 [OpenClaw](https://openclaw.com) 的默认搜索/抓取工具，替代内置的 Brave 搜索。整个流程分 5 步：安装依赖 → 注册 MCP → 创建 Skill → 禁用 Brave → 重启。

### 步骤 1：安装 MCP Server 依赖

MCP Server 使用 [Bun](https://bun.sh) + `@modelcontextprotocol/sdk` 运行：

```bash
# 安装 Bun（如果没有）
curl -fsSL https://bun.sh/install | bash

# 安装 MCP SDK
bun add -g @modelcontextprotocol/sdk zod
```

### 步骤 2：注册到 mcporter

编辑 `~/.mcporter/mcporter.json`，添加 search-stack：

```json
{
  "mcpServers": {
    "search-stack": {
      "command": "/home/your_user/.bun/bin/bun",
      "args": ["run", "/opt/search-stack/proxy/mcp-server.ts"],
      "keepAlive": true,
      "env": {
        "SEARCH_STACK_URL": "http://127.0.0.1:17080",
        "SEARCH_STACK_API_KEY": "your_proxy_api_key"
      }
    }
  }
}
```

> `SEARCH_STACK_API_KEY` 的值就是 `.env` 中的 `PROXY_API_KEY`。
> `command` 填 Bun 的完整路径，用 `which bun` 查看。

（可选）如果要用 [TikHub](https://tikhub.io) 社交媒体 API，添加 `TIKHUB_API_KEY`：

```json
"env": {
  "SEARCH_STACK_URL": "http://127.0.0.1:17080",
  "SEARCH_STACK_API_KEY": "your_proxy_api_key",
  "TIKHUB_API_KEY": "your_tikhub_key"
}
```

验证注册：

```bash
mcporter daemon restart
mcporter list
# 应显示 search-stack (6 tools) healthy
```

### 步骤 3：创建 OpenClaw Skill

创建文件 `~/.openclaw/workspace/skills/web-search/SKILL.md`：

> **核心要点：** OpenClaw 的 AI 通过 `exec` 工具执行 shell 命令来调用 MCP 工具。SKILL.md 里必须用具体的 `mcporter call search-stack.*` 命令格式，不能用抽象的工具名。

<details>
<summary>完整 SKILL.md 示例（点击展开）</summary>

```markdown
---
name: web-search
description: |
  Web search and anti-bot page fetching via search-stack MCP (through mcporter).
  Triggers: "搜索", "search", "查一下", "look up", "抓取网页", "fetch url", "cookie", "登录".
user-invocable: true
metadata:
  openclaw:
    emoji: "🔍"
    requires:
      bins: ["mcporter"]
---

# Web Search & Anti-Bot Fetch

**内置 Brave 搜索已禁用。** 所有网页搜索和抓取通过 `mcporter call search-stack.*` 命令执行。

## 搜索 — web_search

mcporter call search-stack.web_search query="搜索关键词" --output json

常用参数：
- `query="关键词"` — 必填
- `count:5` — 结果数量 (1-10)
- `enrich:true` — 同时抓取每个结果的全文内容（研究模式）
- `max_chars:8000` — enrich 模式下每页最大字符数
- `render:true` — enrich 时用 Chrome 渲染（反爬虫站点）

示例：
  # 基本搜索
  mcporter call search-stack.web_search query="claude opus 4.6 评测" count:5 --output json

  # 深度研究（搜索+抓取全文）
  mcporter call search-stack.web_search query="Python教程" count:3 enrich:true max_chars:5000 --output json

## 抓取网页 — web_fetch

mcporter call search-stack.web_fetch url="https://example.com" --output json

常用参数：
- `url="网址"` — 必填
- `render:true` — 用 headless Chrome 渲染（反爬虫/JS 页面必须开启）
- `max_chars:20000` — 最大抓取字符数
- `bypass_cache:true` — 跳过缓存（更新 cookie 后重试用）

## Cookie 管理与自动引导

### 自动检测：需要 Cookie 的情况

以下**任何一种情况**出现时，都**必须主动引导用户提供 Cookie**：

1. `web_fetch` 返回内容包含 `** LOGIN REQUIRED **`
2. 页面正文内容明显不完整（只有标题/摘要，正文被截断或为空）
3. 返回了反爬提示（如"请登录"、"需要验证"、"请完成安全验证"等）
4. 返回的内容与预期严重不符（如文章页只拿到导航栏/侧边栏）

**引导方式（直接告诉用户这段话）：**

> 这个网站的反爬比较严格，正文没有完整抓到。如果你需要完整内容，可以提供该网站的 Cookie：
> 1. 在浏览器中打开该网址并登录
> 2. 按 F12 → Network 标签 → 刷新页面
> 3. 点击任意请求，找到请求头中的 `Cookie:` 一行
> 4. 复制整个值发给我，我会自动保存并重新抓取

**不要**：
- 不要解释文章内容来代替抓取失败的事实
- 不要跳过 Cookie 引导直接回答"抓不到"
- 不要让用户自己去搞技术细节

### 用户主动提供 Cookie

当用户发送消息中包含 Cookie 信息时，**自动识别并处理**：

**场景 A：用户同时发了网址和 Cookie**
→ 从网址提取域名，自动保存 Cookie

**场景 B：用户只发了 Cookie，没给网址**
→ 询问用户这个 Cookie 对应哪个网站

**场景 C：用户发了网址但没有 Cookie**
→ 给出 F12 获取 Cookie 的步骤

Cookie 管理命令：
  # 查看已配置域名
  mcporter call search-stack.cookies_list --output json

  # 保存 Cookie
  mcporter call search-stack.cookies_update domain="zhihu.com" raw="sid=abc; token=xyz" --output json

  # 删除域名 Cookie
  mcporter call search-stack.cookies_delete domain="zhihu.com" --output json

## 使用规则

1. **搜索一律用 `search-stack.web_search`** — 内置搜索已禁用
2. **普通网页可先试内置 `web_fetch`**，失败后用 `search-stack.web_fetch`
3. **反爬虫站用 `search-stack.web_fetch render:true`**
4. **深度研究用 `search-stack.web_search enrich:true`** — 搜索+抓全文一步到位
5. 遇到 `LOGIN REQUIRED` 或**正文不完整** → **必须主动引导用户提供 Cookie**
6. 用户发送 Cookie 文本时 → **自动识别、提取域名、保存**，不要再问"要不要保存"
7. **始终加 `--output json`** 以便解析结果
8. **命令超时处理**：最多重试 1 次，仍然失败则告知用户并建议换种方式
```

</details>

### 步骤 4：禁用内置 Brave 搜索

编辑 `~/.openclaw/openclaw.json`，添加：

```json
{
  "search": {
    "enabled": false
  }
}
```

### 步骤 5：重启 OpenClaw

```bash
sudo systemctl restart openclaw
```

> **重要：** 如果 AI 仍在使用旧的 Brave 搜索，需要归档旧 session。OpenClaw 的会话上下文会缓存之前的工具模式，即使 SKILL.md 已更新，旧 session 仍会沿用旧行为。详见下方「常见问题 → AI 不使用 search-stack」。

### MCP Server 提供的工具

| 工具 | 说明 |
|------|------|
| `web_search` | 多引擎搜索，支持 `enrich` 全文抓取 |
| `web_fetch` | 抓取网页正文，支持 Chrome 渲染 |
| `cookies_list` | 列出已配置 Cookie 的域名 |
| `cookies_update` | 添加/更新域名 Cookie（支持 raw 字符串粘贴） |
| `cookies_delete` | 删除域名 Cookie |
| `tikhub_call` | 调用 TikHub 社交媒体 API（需配置 Key，按需使用） |

### Cookie 工作流实战

以知乎为例，完整的 Cookie 工作流如下：

```
用户: "帮我看看这个网页 https://zhuanlan.zhihu.com/p/xxxx"

AI: 调用 web_fetch → 正文不完整（只有标题/摘要）
AI: "这个网站的反爬比较严格，正文没有完整抓到。
     如果你需要完整内容，可以提供该网站的 Cookie：
     1. 在浏览器中打开该网址并登录
     2. 按 F12 → Network 标签 → 刷新页面
     3. 找到请求头中的 Cookie: 一行
     4. 复制整个值发给我"

用户: "z_c0=xxx; _xsrf=yyy; d_c0=zzz ..."

AI: 自动提取域名 zhihu.com → cookies_update → 保存成功
AI: 用 bypass_cache:true 重新抓取 → 拿到完整文章内容
```

---

## 常见问题 & 踩坑指南

### 部署相关

**Q: SearXNG 搜索返回 403 / 502 / 空结果**

这是最常见的坑。SearXNG 默认**禁用 JSON 格式的搜索 API**，search-stack 用 `?format=json` 调用时会被返回 403 Forbidden。

**解决方案：** 确保 `searxng/settings.yml` 中包含：

```yaml
search:
  formats:
    - html
    - json     # ← 没有这行就会 403
```

修改后重启容器：

```bash
docker compose -f search-stack.yml restart searxng
```

> **为什么不容易发现？** search-stack 的 proxy 会把 SearXNG 的 403 当作"不可用"，静默跳过并 fallback 到 Tavily/Serper。你可能一直以为 SearXNG 正常，其实从来没用上。用 `provider: "searxng"` 强制指定测试一次就能发现。

**Q: SearXNG 首次启动权限问题**

SearXNG 容器内使用 UID 977 运行。如果挂载目录权限不对会启动失败：

```bash
sudo chown -R 977:977 /opt/search-stack/searxng/
docker compose -f search-stack.yml restart searxng
```

**Q: Browserless 超时或崩溃**

Browserless 默认最多 5 个并发 Chrome 会话。如果频繁超时，检查内存（至少 2GB）：

```bash
docker stats browserless
```

可以调整 `search-stack.yml` 中的 `MAX_CONCURRENT_SESSIONS`。

**Q: Redis 连接失败**

确认 `REDIS_PASSWORD` 在 `.env` 中设置且非空：

```bash
docker exec search-redis redis-cli -a YOUR_REDIS_PASSWORD ping
# 应返回 PONG
```

**Q: search-proxy 启动时报错 `redis.exceptions.ConnectionError`**

`search-proxy` 依赖 Redis 和 SearXNG 先启动。`docker compose` 的 `depends_on` + `healthcheck` 通常能处理，但如果 Redis 启动慢：

```bash
docker compose -f search-stack.yml restart search-proxy
```

### MCP Server 相关

**Q: `mcporter list` 显示 search-stack 不健康**

逐步排查：

1. 确认 Docker 容器在运行：`docker compose -f search-stack.yml ps`
2. 确认 API 可达：`curl -H "X-API-Key: KEY" http://127.0.0.1:17080/health`
3. 确认 Bun 路径正确：`which bun`
4. 直接运行看报错：`SEARCH_STACK_URL=http://127.0.0.1:17080 SEARCH_STACK_API_KEY=your_key bun run /opt/search-stack/proxy/mcp-server.ts`

**Q: `z.record()` / `schema._zod` 错误**

MCP SDK v1.26.0 + Zod v4 的已知问题。`z.record()` 在 `tools/list` 序列化时会报 `Cannot read properties of undefined (reading '_zod')`。

解决方案（本项目已处理）：
- 参数用 `z.string()` 代替 `z.record()`，在 handler 中 `JSON.parse()`
- 需要同时接受对象和字符串的参数用 `z.any()`

**Q: mcporter 传 JSON 参数报 "expected string, received object"**

mcporter 会自动把 JSON 字符串解析成对象再传给 MCP 工具。如果 schema 定义为 `z.string()` 就会验证失败。

解决方案（本项目已处理）：用 `z.any()` 并在 handler 中同时处理两种类型：

```typescript
const rawArgs = params.arguments as unknown;
if (typeof rawArgs === "object" && rawArgs !== null) {
  args = rawArgs as Record<string, unknown>;
} else {
  args = JSON.parse((rawArgs as string) || "{}");
}
```

### OpenClaw 集成相关

**Q: AI 不使用 search-stack，还在用内置 Brave 搜索**

三个检查点：

1. 确认 Brave 已禁用：`~/.openclaw/openclaw.json` 中 `"search": { "enabled": false }`
2. 确认 SKILL.md 存在：`ls ~/.openclaw/workspace/skills/web-search/SKILL.md`
3. **（最关键）归档旧 session：** OpenClaw 的会话上下文（可能几十万 token）会缓存之前的工具调用模式。即使 SKILL.md 已更新，旧 session 仍会沿用旧行为。必须归档：

```bash
# 找到活跃 session
ls -lt ~/.openclaw/agents/main/sessions/*.jsonl | head -3

# 归档（重命名，不要删除）
mv ~/.openclaw/agents/main/sessions/SESSION_ID.jsonl \
   ~/.openclaw/agents/main/sessions/SESSION_ID.jsonl.archived

# 从注册表中移除对应条目
# 编辑 ~/.openclaw/agents/main/sessions/sessions.json
# 找到包含该 SESSION_ID 的 key，删除整个条目

# 重启
sudo systemctl restart openclaw
```

新会话启动后 AI 会重新读取 SKILL.md，使用 `mcporter call` 命令。

**Q: AI 抓到了部分内容但没引导用户提供 Cookie**

SKILL.md 中必须明确写出**所有**触发 Cookie 引导的条件：

- 返回 `** LOGIN REQUIRED **`
- 正文内容不完整（只有标题/摘要，正文被截断或为空）
- 出现反爬提示（"请登录"、"需要验证"等）
- 内容与预期严重不符（文章页只拿到侧边栏）

同时要明确告诉 AI **"不要做什么"**——不要用解释文章内容来代替抓取失败，不要跳过引导。如果只写 `LOGIN REQUIRED` 一个条件，AI 在拿到部分内容时不会触发引导。

**Q: AI 用 `exec` + `curl` 调用 Brave 而不是 `mcporter call`**

OpenClaw 的 AI 通过 `exec` 工具执行 shell 命令来调用 MCP。SKILL.md 中必须使用具体的命令格式：

```bash
mcporter call search-stack.web_search query="关键词" --output json
```

不能写成抽象的 `search-stack.web_search(query="关键词")`，AI 不会自己翻译成 shell 命令。

**Q: SKILL.md 更新后 AI 行为没变化**

同上——旧 session 缓存了旧的 SKILL.md 内容。归档旧 session 后重启即可。

---

## API 文档

所有请求需携带 `X-API-Key` 头部。

### `GET /health`

健康检查。

```json
{
  "ok": true,
  "redis": true,
  "order": ["tavily", "serper", "searxng"],
  "browserless_configured": true,
  "dedupe": { "enabled": true, "max_per_host": 2 }
}
```

### `POST /search`

Web 搜索。

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `query` | string | 必填 | 搜索关键词 |
| `count` | int | 5 | 返回结果数（1-10） |
| `provider` | string | 自动 | 强制指定：`tavily` / `serper` / `searxng` |
| `enrich` | bool | false | 抓取每条结果的网页全文 |
| `max_chars` | int | 8000 | enrich 时每页最大字符数 |
| `render` | bool | true | 用无头浏览器渲染 |
| `concurrency` | int | 3 | enrich 并发抓取数 |

```bash
# 基础搜索
curl -s -X POST http://127.0.0.1:17080/search \
  -H "X-API-Key: KEY" -H "Content-Type: application/json" \
  -d '{"query": "Docker best practices", "count": 5}'

# 搜索 + 抓取全文（深度研究）
curl -s -X POST http://127.0.0.1:17080/search \
  -H "X-API-Key: KEY" -H "Content-Type: application/json" \
  -d '{"query": "Python asyncio", "count": 3, "enrich": true}'

# 强制使用 SearXNG（免费，不消耗 API 额度）
curl -s -X POST http://127.0.0.1:17080/search \
  -H "X-API-Key: KEY" -H "Content-Type: application/json" \
  -d '{"query": "AI news", "count": 5, "provider": "searxng"}'
```

**返回示例：**

```json
{
  "query": "Docker best practices",
  "count": 5,
  "cached": false,
  "provider": "tavily",
  "results": [
    {
      "title": "Docker Best Practices",
      "url": "https://example.com/docker",
      "snippet": "Top 10 Docker best practices for production...",
      "source": "tavily"
    }
  ]
}
```

### `POST /fetch`

抓取网页正文。

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `url` | string | 必填 | 目标 URL |
| `render` | bool | true | 用无头浏览器渲染 |
| `max_chars` | int | 20000 | 最大提取字符数 |
| `timeout` | float | 25 | 超时秒数 |
| `headers` | object | null | 自定义请求头 |
| `bypass_cache` | bool | false | 跳过缓存（更新 Cookie 后用） |

**返回示例：**

```json
{
  "cached": false,
  "url": "https://example.com/",
  "status_code": 200,
  "render": false,
  "title": "Example Domain",
  "text": "This domain is for use in illustrative examples..."
}
```

当页面需要登录时：

```json
{
  "needs_login": true,
  "has_cookies": false
}
```

### Cookie 管理

动态管理域名 Cookie，无需重启。Cookie 自动注入 Browserless 渲染请求。

```bash
# 列出所有域名
GET /cookies

# 添加/更新 — Raw 字符串（直接从浏览器复制粘贴）
PUT /cookies/zhihu.com
  {"raw": "z_c0=xxx; _xsrf=yyy; d_c0=zzz"}

# 添加/更新 — JSON 数组
PUT /cookies/zhihu.com
  {"cookies": [{"name":"z_c0","value":"xxx"}, {"name":"_xsrf","value":"yyy"}]}

# 删除
DELETE /cookies/zhihu.com

# 从 cookies.json 重新加载
POST /cookies/reload
```

---

## 配置参考

### 环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `TAVILY_API_KEY` | - | Tavily API Key |
| `SERPER_API_KEY` | - | Serper (Google) API Key |
| `ORDER` | `tavily,serper,searxng` | 搜索引擎优先级 |
| `API_KEYS` | - | 代理鉴权 Key（逗号分隔支持多个） |
| `RATE_LIMIT_PER_MIN` | `60` | 每分钟请求上限 |
| `CACHE_TTL` | `900` | 缓存过期秒数 |
| `FETCH_TIMEOUT` | `25` | 抓取超时秒数 |
| `MAX_FETCH_BYTES` | `2000000` | 单页最大抓取字节数 |
| `FETCH_DEFAULT_RENDER` | `true` | 默认启用浏览器渲染 |
| `ALLOW_DOMAINS` | - | 域名白名单（逗号分隔） |
| `BLOCK_DOMAINS` | - | 域名黑名单（逗号分隔） |
| `DEDUPE` | `true` | URL 去重 |
| `MAX_PER_HOST` | `2` | 同域名最多返回结果数 |

### MCP Server 环境变量

通过 mcporter 的 `env` 配置传入：

| 变量 | 说明 |
|------|------|
| `SEARCH_STACK_URL` | REST API 地址（默认 `http://127.0.0.1:17080`） |
| `SEARCH_STACK_API_KEY` | 同 `PROXY_API_KEY` |
| `TIKHUB_API_KEY` | （可选）TikHub 社交媒体 API Key |

### SearXNG 配置

SearXNG 配置文件位于 `searxng/settings.yml`。关键配置：

```yaml
use_default_settings: true

server:
  secret_key: "your_secret"      # 必填，随机字符串
  limiter: false                  # 关闭限流（内部服务，无需限流）

search:
  safe_search: 0
  formats:
    - html
    - json                        # 必须开启 JSON 格式

outgoing:
  request_timeout: 10.0
  max_request_timeout: 20.0
```

详细配置参考 [SearXNG 文档](https://docs.searxng.org/)。

---

## 项目结构

```
search-stack/
├── search-stack.yml          # Docker Compose 编排
├── .env                      # 环境变量（密钥，不入 Git）
├── .env.example              # 环境变量模板
├── proxy/
│   ├── Dockerfile            # 代理服务镜像
│   ├── app.py                # FastAPI 主程序（REST API）
│   ├── mcp-server.ts         # MCP Server（stdio，Bun 运行）
│   ├── cookies.json          # Cookie 存储（运行时自动更新）
│   ├── cookies.json.example  # Cookie 格式示例
│   └── requirements.txt      # Python 依赖
└── searxng/
    ├── settings.yml          # SearXNG 配置（首次启动自动生成）
    └── settings.yml.example  # SearXNG 配置模板
```

## 安全说明

- 所有内部服务（Redis、SearXNG、Browserless）不暴露宿主机端口，仅通过 Docker 内部网络通信
- Redis 启用密码认证
- 内置 SSRF 防护：拒绝访问私网 IP（127.0.0.0/8、10.0.0.0/8、172.16.0.0/12、192.168.0.0/16、169.254.0.0/16）
- URL 自动规范化，去除追踪参数
- API Key 鉴权 + 每分钟滑动窗口限流
- `.env` 文件包含敏感密钥，**务必加入 `.gitignore`**

### 反向代理（生产部署）

服务默认只监听 `127.0.0.1:17080`。生产环境如需外网访问，通过 Nginx 反向代理 + HTTPS：

```nginx
location /search-stack/ {
    proxy_pass http://127.0.0.1:17080/;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_read_timeout 60s;
    proxy_send_timeout 60s;
}
```

## License

MIT
