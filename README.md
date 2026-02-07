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
- **登录/反爬检测** — 多维度启发式检测：HTTP 状态码（401/403）、文本关键词（中英日）、页面标题、HTML 结构（密码框、CAPTCHA 嵌入、meta refresh 重定向）、SPA 登录墙，返回 `needs_login` 标记引导 Cookie 更新
- **Cookie Catcher** — 浏览器内远程登录：通过 WebSocket + CDP Screencast 在 Web UI 中操控远程 Chrome 完成登录，一键保存 Cookie
- **SSRF 防护** — 拒绝访问私网 IP（127/10/172.16/192.168/169.254）
- **URL 去重** — 自动去除追踪参数（utm_*、fbclid 等），同域名结果限制
- **Redis 缓存** — 15 分钟 TTL，重复查询即时返回
- **API Key 鉴权 + 限流** — 滑动窗口限流
- **MCP Server** — stdio 模式 MCP Server（`mcp-server.ts`），可通过 mcporter 注册供 OpenClaw 等 Agent 使用
- **TikHub 社交媒体 API** — 可选集成，代理 TikHub 803 个社交平台工具（抖音、TikTok、微博等），内置自动回退
- **HTTP 代理** — 支持 HTTP / SOCKS5 代理，用于反爬固定 IP 或翻墙访问被墙网站（YouTube 等）
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

Search Stack 可以作为 [OpenClaw](https://openclaw.com) 的默认搜索/抓取工具，替代内置的 Brave 搜索。有两种集成方式：

### 方式一：原生插件（推荐）

原生插件将工具直接注册到 AI 的工具列表中，运行在 OpenClaw 进程内。相比 MCP+mcporter 方式：
- **超时可控**：AbortSignal 超时返回异常，AI 总能看到错误信息（不会 SIGKILL → 零输出）
- **延迟更低**：无需启动子进程
- **更可靠**：不依赖 mcporter daemon

#### 步骤 1：安装插件

```bash
# 用 --link 符号链接安装（更新时无需重装）
openclaw plugins install --link /opt/search-stack/plugin/
```

#### 步骤 2：配置

编辑 `~/.openclaw/openclaw.json`，在 `plugins.entries` 中添加：

```json
{
  "plugins": {
    "entries": {
      "search-stack": {
        "enabled": true,
        "config": {
          "apiUrl": "http://127.0.0.1:17080",
          "apiKey": "your_proxy_api_key",
          "tikhubApiKey": "your_tikhub_key"
        }
      }
    }
  }
}
```

> `apiKey` 的值就是 `.env` 中的 `PROXY_API_KEY`。
> `tikhubApiKey` 可选，填入 [TikHub](https://tikhub.io) API Key 可启用社交媒体 API。
> **注意：** 配置必须放在 `config` 嵌套对象内，不能直接放在 `search-stack` 下。

#### 步骤 3：禁用内置 Brave 搜索

编辑 `~/.openclaw/openclaw.json`：

```json
{
  "tools": {
    "web": {
      "search": {
        "enabled": false
      }
    }
  }
}
```

#### 步骤 4：创建 Skill 文件

```bash
mkdir -p ~/.openclaw/workspace/skills/web-search/
cp /opt/search-stack/skill-template/SKILL.md ~/.openclaw/workspace/skills/web-search/SKILL.md
```

Skill 指导 AI 何时以及如何使用搜索工具（两步法原则、Cookie 工作流、TikHub 优先级等）。模板文件位于 `skill-template/SKILL.md`，可根据需要自行修改。

#### 步骤 5：重启并验证

```bash
sudo systemctl restart openclaw

# 验证插件加载
openclaw plugins list  # 应显示 search-stack: loaded

# 验证工具注册（通过 AI 对话或 gateway API）
# AI 应能直接看到 web_search 等工具，无需 exec
```

> **重要：** 如果 AI 仍在使用旧方式，需要归档旧 session。OpenClaw 的会话上下文会缓存之前的工具模式，即使配置已更新，旧 session 仍会沿用旧行为。详见下方「常见问题 → AI 不使用 search-stack」。

### 方式二：MCP + mcporter（备选）

适用于不想安装原生插件、或需要在 OpenClaw 以外的环境使用的场景。

#### 步骤 1：安装 MCP Server 依赖

MCP Server 使用 [Bun](https://bun.sh) + `@modelcontextprotocol/sdk` 运行：

```bash
# 安装 Bun（如果没有）
curl -fsSL https://bun.sh/install | bash

# 安装 MCP SDK
bun add -g @modelcontextprotocol/sdk zod
```

#### 步骤 2：注册到 mcporter

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
        "SEARCH_STACK_API_KEY": "your_proxy_api_key",
        "TIKHUB_API_KEY": "your_tikhub_key"
      }
    }
  }
}
```

验证注册：

```bash
mcporter daemon restart
mcporter list
# 应显示 search-stack (6 tools) healthy
```

#### 步骤 3：创建 Skill 并重启

创建 `~/.openclaw/workspace/skills/web-search/SKILL.md`（使用 mcporter exec 调用格式），禁用 Brave 搜索，重启 OpenClaw。

> **注意：** MCP+mcporter 方式中，AI 通过 `exec` 工具执行 `mcporter call search-stack.*` 命令。超时时 SIGKILL 会导致零输出，AI 可能认为"搜索引擎挂了"。推荐使用原生插件方式避免此问题。

### 异地部署（OpenClaw 和 Search Stack 在不同机器）

适用场景：Search Stack 运行在服务器 A（有公网域名），OpenClaw 运行在服务器 B，需要远程调用 Search Stack 的 API。

```
服务器 A (search-stack)              服务器 B (openclaw)
┌──────────────────────┐             ┌──────────────────────┐
│  Docker 四件套        │   HTTPS     │  OpenClaw            │
│  search-proxy :17080 │◄────────────│  search-stack 插件    │
│  Redis / SearXNG     │             │                      │
│  Browserless         │             │  只需要 plugin/ 目录  │
└──────────────────────┘             └──────────────────────┘
```

#### 步骤 1：服务器 A — 配置反向代理（HTTPS）

Search Stack 默认只监听 `127.0.0.1:17080`，异地访问需要通过 Nginx 反向代理暴露 HTTPS 端口。

Nginx 配置示例（假设域名为 `search.example.com`）：

```nginx
location /search-stack/ {
    proxy_pass http://127.0.0.1:17080/;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_read_timeout 60s;
    proxy_send_timeout 60s;

    # Cookie Catcher WebSocket 支持
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
}
```

> **安全提醒：** API Key 会在请求头中明文传输，**必须使用 HTTPS**。建议用 Certbot 自动申请 Let's Encrypt 证书。

#### 步骤 2：服务器 B — 获取插件代码

只需要 `plugin/` 和 `skill-template/` 两个目录，不需要安装 Docker 或完整项目：

```bash
# 方法一：克隆完整仓库（简单）
git clone https://github.com/pinkpills/search-stack.git /opt/search-stack
cd /opt/search-stack/plugin && npm install

# 方法二：只下载需要的目录（轻量）
mkdir -p /opt/search-stack && cd /opt/search-stack
# 下载 plugin/ 和 skill-template/
git clone --depth 1 --filter=blob:none --sparse \
  https://github.com/pinkpills/search-stack.git .
git sparse-checkout set plugin skill-template
cd plugin && npm install
```

#### 步骤 3：服务器 B — 安装插件到 OpenClaw

```bash
openclaw plugins install --link /opt/search-stack/plugin/
```

#### 步骤 4：服务器 B — 配置远程 API 地址

编辑 `~/.openclaw/openclaw.json`：

```json
{
  "plugins": {
    "entries": {
      "search-stack": {
        "enabled": true,
        "config": {
          "apiUrl": "https://search.example.com/search-stack",
          "apiKey": "your_proxy_api_key",
          "publicUrl": "https://search.example.com/search-stack",
          "tikhubApiKey": "your_tikhub_key"
        }
      }
    }
  },
  "tools": {
    "web": {
      "search": {
        "enabled": false
      }
    }
  }
}
```

配置说明：

| 字段 | 说明 |
|------|------|
| `apiUrl` | 服务器 A 的 Search Stack API 地址（通过 Nginx 代理后的 HTTPS URL） |
| `apiKey` | 服务器 A `.env` 中的 `PROXY_API_KEY` |
| `publicUrl` | Cookie Catcher 链接中使用的公网 URL（用户浏览器需要能访问），通常与 `apiUrl` 相同 |
| `tikhubApiKey` | （可选）TikHub API Key |

#### 步骤 5：服务器 B — 创建 Skill 并重启

```bash
mkdir -p ~/.openclaw/workspace/skills/web-search/
cp /opt/search-stack/skill-template/SKILL.md ~/.openclaw/workspace/skills/web-search/SKILL.md
sudo systemctl restart openclaw
```

#### 验证

在 OpenClaw 中对话测试：

```
用户: "搜索一下 Claude Opus 4.6 评测"
AI:   调用 web_search → 返回结果（来自远程 Search Stack）

用户: "打开第一条链接看看全文"
AI:   调用 page_fetch → 返回全文（远程 Browserless 渲染）
```

如果工具调用失败，检查：
1. 服务器 B 能否访问 `apiUrl`：`curl -H "X-API-Key: KEY" https://search.example.com/search-stack/health`
2. 插件是否加载：`openclaw plugins list`
3. 旧 session 缓存：归档旧 session 后重启（详见「常见问题 → AI 不使用 search-stack」）

#### 多机并发与资源控制

多台机器共用一个 Search Stack 时，Browserless Chrome 是主要瓶颈——每个并发渲染会话占用约 400-500MB 内存。默认配置已针对 3 台客户端优化：

```yaml
# search-stack.yml — browserless 部分
MAX_CONCURRENT_SESSIONS=10    # 最多 10 个 Chrome 同时运行
MAX_QUEUE_LENGTH=30           # 超出并发后排队上限
CONNECTION_TIMEOUT=120000     # 单会话超时 2 分钟，防止长期占位
deploy:
  resources:
    limits:
      memory: 4g              # 内存硬上限，超了 OOM 自动重启，不拖垮整机
```

按客户端数量调整建议：

| 客户端数 | `MAX_CONCURRENT_SESSIONS` | `memory` | 服务器内存建议 |
|----------|--------------------------|----------|---------------|
| 1-2 台 | 5 | 2g | 4GB+ |
| 3-5 台 | 10 | 4g | 8GB+ |
| 5-10 台 | 20 | 8g | 16GB+ |

> 超出并发上限时，Browserless 会将请求排队等待；队列也满时返回 429，`page_fetch` 会报错但不影响搜索功能（`web_search` 不依赖 Chrome）。Cookie Catcher 另有 2 会话硬限制，多人同时登录需排队。

### 工具列表

无论使用原生插件还是 MCP 方式，提供的工具相同：

| 工具 | 说明 |
|------|------|
| `web_search` | 多引擎搜索，支持 `enrich` 全文抓取 |
| `page_fetch` | 抓取网页正文，支持 Cookie 注入 + Chrome 渲染 + 登录检测 |
| `cookies_list` | 列出已配置 Cookie 的域名 |
| `cookies_update` | 添加/更新域名 Cookie（支持 raw 字符串粘贴） |
| `cookies_delete` | 删除域名 Cookie |
| `cookie_catcher_link` | 生成远程浏览器登录链接（Cookie Catcher） |
| `tikhub_call` | 调用 TikHub 社交媒体 API（需配置 Key，按需使用） |

> **注意：** 抓取工具名为 `page_fetch` 而非 `web_fetch`。这是为了避免与 OpenClaw 内置的 `web_fetch` 工具名冲突。内置 `web_fetch` 不支持 Cookie 注入和 Chrome 渲染，使用同名会导致 AI 调用错误的工具。

### Cookie 工作流实战

有两种方式获取 Cookie：

**方式一：手动复制粘贴（适用于桌面端）**

```
用户: "帮我看看这个网页 https://zhuanlan.zhihu.com/p/xxxx"

AI: 调用 page_fetch → 正文不完整（只有标题/摘要）
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

**方式二：Cookie Catcher 远程登录（适用于复杂登录流程）**

```
1. 浏览器打开：http://YOUR_HOST:17080/cookie-catcher?key=API_KEY&url=https://threads.net
2. 在远程 Chrome 画面中完成登录（支持鼠标/键盘/触屏操作）
3. 点击 "Save Cookies" → 自动保存到 cookies.json
4. 后续 /fetch 请求自动注入 Cookie
```

适合需要 OAuth 跳转、二维码扫码、手机验证码等复杂登录场景。

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

**Q: Threads/Instagram 等 SPA 网站抓取失败，提示"JS SPA requiring Chrome"**

这通常**不是**缺少 Chrome 的问题（Browserless 默认就在运行）。真实原因是 Cookie 过期：

1. Browserless 用 Chrome 渲染 + 注入了 Cookie，但 session 已失效
2. React SPA 没有渲染实际内容，返回登录页
3. 登录页文本很短，AI 误以为是 JS 渲染失败

**解决方案：** 从浏览器重新导出 Cookie（确保已登录），通过 `cookies_update` 更新后用 `bypass_cache: true` 重试。

`detect_needs_login` 支持多维度检测：HTTP 状态码（401/403）、文本关键词（中英日）、页面标题、HTML 密码框/CAPTCHA/meta refresh、SPA 登录墙（Threads/Instagram/Facebook），会返回明确的 `needs_login: true` 提示。

也可以使用 Cookie Catcher 远程登录：打开 `/cookie-catcher?key=API_KEY&url=TARGET_URL`，在远程 Chrome 中完成登录后一键保存 Cookie。

**Q: AI 用 `exec` + `curl` 调用 Brave 而不是 `mcporter call`**

OpenClaw 的 AI 通过 `exec` 工具执行 shell 命令来调用 MCP。SKILL.md 中必须使用具体的命令格式：

```bash
mcporter call search-stack.web_search query="关键词" --output json
```

不能写成抽象的 `search-stack.web_search(query="关键词")`，AI 不会自己翻译成 shell 命令。

**Q: SKILL.md 更新后 AI 行为没变化**

这是最容易踩的坑之一。原因和解决方案：

**背景机制：** OpenClaw 有 Skills Watcher（默认开启，`skills.load.watch: true`），会监控 SKILL.md 文件变化并 bump 版本号。但这只刷新 skill **列表**（哪些 skill 可用），不强制 AI 重新读取 SKILL.md 内容。

**为什么改了 SKILL.md 不生效：**

1. AI 不是每轮都 read SKILL.md — 它只在 session 首轮或判断需要时才读
2. 读过的内容会缓存在 session 上下文中（可能几十万 token）
3. 更关键的是，AI 大部分时候**只看工具的 description 字段**（注册时写死的短文本），不看 SKILL.md

**正确的解决方法（按推荐程度排序）：**

1. **修改工具的 description**（最有效）— 工具 description 始终对 AI 可见，修改 `plugin/index.ts` 中的 description 字段 → 重启 OpenClaw → 新 session 立即生效。关键行为约束应写在 description 里，不只写在 SKILL.md
2. **归档旧 session** — 强制新 session 重新加载所有上下文：
   ```bash
   # 归档所有活跃 session
   for f in ~/.openclaw/agents/main/sessions/*.jsonl; do
     mv "$f" "$f.archived"
   done

   # 清空注册表
   echo '{"sessions":[]}' > ~/.openclaw/agents/main/sessions/sessions.json

   # 重启
   sudo systemctl restart openclaw
   ```
3. **等 Skills Watcher 生效** — 如果只改了 SKILL.md 的补充说明（不涉及核心行为），可以等 AI 在新一轮对话中被触发重新 read SKILL.md

**最佳实践：** 核心行为约束（如"先用 A 工具再用 B 工具"）写在工具 description 里，详细流程和示例写在 SKILL.md 里。这样即使 AI 没 read SKILL.md，工具 description 也能兜底。

**Q: 插件代码（index.ts）更新后需要做什么**

插件运行在 OpenClaw 进程内，代码变更后需要：

```bash
# 重启 OpenClaw（重新加载插件代码）
sudo systemctl restart openclaw
```

如果同时改了 SKILL.md，建议一并归档旧 session（见上一条）。

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

当页面需要登录或被反爬拦截时，返回 `needs_login: true`：

```json
{
  "needs_login": true,
  "has_cookies": false
}
```

`has_cookies: true` 时表示已有 Cookie 但已过期，需要重新导出。

检测规则覆盖以下信号（按优先级排列）：

| 信号 | 条件 | 示例 |
|------|------|------|
| HTTP 401 | 直接判定 | API 端点未认证 |
| HTTP 403 + 短内容 | text < 2000 字符 | 访问被拒绝页面 |
| 文本登录关键词 | 1 hit + < 500 字符，或 2+ hits | "请登录"、"sign in to continue"、"verify you are human" |
| 页面标题含登录词 | + text < 2000 字符 | `<title>Sign In - Example</title>` |
| HTML 密码输入框 | + text < 3000 字符 | `<input type="password">` |
| Meta refresh → 登录 URL | 直接判定 | `<meta http-equiv="refresh" content="0;url=/login">` |
| CAPTCHA 嵌入 | + text < 1000 字符 | reCAPTCHA、hCaptcha、Cloudflare Turnstile |
| 空壳备案页 | 2+ hits + < 800 字符 | 仅含 ICP 备案号的页面（小红书等） |

支持中文、英文、日文登录关键词，以及 OAuth 提示（"continue with Google"）、付费墙（"subscribe to continue"）、Cloudflare 验证（"checking your browser"）等。

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

### Cookie Catcher（远程浏览器登录）

对于无法直接复制 Cookie 的场景（如手机端、复杂 OAuth 流程），Cookie Catcher 提供 Web UI 远程操控 Chrome 完成登录：

```
浏览器访问：GET /cookie-catcher?key=YOUR_API_KEY[&url=https://target-site.com]
```

**工作流程：**

1. 浏览器打开 `/cookie-catcher?key=API_KEY`，建立 WebSocket 连接
2. 在地址栏输入目标网站 URL，点击 Go
3. 通过 CDP Screencast 实时显示远程 Chrome 画面（JPEG 流）
4. 用户在画面上操作（鼠标点击、键盘输入、滚动）完成登录
5. 点击 "Save Cookies" 一键提取并保存当前域名的所有 Cookie
6. Cookie 自动写入 `cookies.json`，后续 `/fetch` 渲染请求自动注入

**技术细节：**

| 参数 | 值 |
|------|-----|
| WebSocket 端点 | `WS /cookie-catcher/ws?key=API_KEY` |
| 最大并发会话 | 2 |
| 会话超时 | 10 分钟自动关闭 |
| 画面分辨率 | 1280 x 800 |
| 画面格式 | JPEG，quality=60 |
| 输入支持 | 鼠标（点击/移动/滚轮）、键盘、触屏 |

**WebSocket 消息协议：**

客户端 → 服务端：
```json
{"type": "navigate", "url": "https://example.com"}
{"type": "mouse", "action": "mousePressed", "x": 100, "y": 200, "button": "left"}
{"type": "key", "action": "keyDown", "key": "a", "code": "KeyA", "text": "a"}
{"type": "scroll", "x": 640, "y": 400, "deltaX": 0, "deltaY": 100}
{"type": "save_cookies", "domain": "example.com"}
{"type": "close"}
```

服务端 → 客户端：
```json
{"type": "frame", "data": "<base64 JPEG>"}
{"type": "url", "url": "https://example.com/dashboard"}
{"type": "title", "title": "Dashboard"}
{"type": "cookies_saved", "domain": "example.com", "count": 15, "names": ["session", "token", ...]}
{"type": "error", "message": "Too many active sessions (max 2)"}
{"type": "closed"}
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
| `PROXY_URL` | - | HTTP 代理，详见 [HTTP 代理](#http-代理可选) |

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
├── plugin/
│   ├── openclaw.plugin.json  # OpenClaw 原生插件 manifest
│   └── index.ts              # 插件入口（推荐集成方式）
├── skill-template/
│   └── SKILL.md              # OpenClaw Skill 模板（复制到 ~/.openclaw/workspace/skills/web-search/）
├── proxy/
│   ├── Dockerfile            # 代理服务镜像
│   ├── app.py                # FastAPI 主程序（REST API）
│   ├── cookie_catcher.py     # Cookie Catcher（远程浏览器 CDP 会话管理）
│   ├── mcp-server.ts         # MCP Server（stdio，备选集成方式）
│   ├── cookies.json          # Cookie 存储（运行时自动更新）
│   ├── cookies.json.example  # Cookie 格式示例
│   ├── requirements.txt      # Python 依赖
│   └── static/
│       └── cookie-catcher.html  # Cookie Catcher Web UI
└── searxng/
    ├── settings.yml          # SearXNG 配置（首次启动自动生成）
    └── settings.yml.example  # SearXNG 配置模板
```

## HTTP 代理（可选）

配置 `PROXY_URL` 后，所有出站请求通过代理发出，适用于：

- **反爬固定 IP** — 目标网站看到代理 IP 而非服务器真实 IP
- **翻墙出海** — 中国区域服务器访问 YouTube、Google 等被墙网站

### 配置

在 `.env` 中设置：

```bash
# HTTP 代理
PROXY_URL=http://host:port

# 带认证的 HTTP 代理
PROXY_URL=http://user:pass@host:port

# SOCKS5 代理
PROXY_URL=socks5://host:port

# 带认证的 SOCKS5 代理
PROXY_URL=socks5://user:pass@host:port
```

不配置或留空则所有请求直连，行为与之前完全一致。

### 代理覆盖范围

| 请求类型 | 走代理？ | 说明 |
|----------|----------|------|
| httpx 直连抓取（`render=false`） | ✅ | `http_client` 带 `proxy` 参数 |
| Tavily / Serper API 调用 | ✅ | 同上 |
| SearXNG → Google/DuckDuckGo 等 | ✅ | 通过 `HTTP_PROXY`/`HTTPS_PROXY` 环境变量传递给 SearXNG 容器 |
| Browserless Chrome 渲染（`render=true`） | ⚠️ 有条件 | 通过 Chrome `--proxy-server` 启动参数，**不支持带认证的代理**（见下方说明） |
| 内部容器间通信（Redis、SearXNG API、Browserless API） | ❌ | 使用独立的 `http_internal` 客户端，永远不走代理 |

### Chrome 渲染的代理限制

Chrome 的 `--proxy-server` 启动参数只接受 `scheme://host:port` 格式，**没有传递用户名密码的机制**。因此：

| 代理类型 | httpx 直连 | Chrome 渲染 |
|----------|-----------|-------------|
| 无认证（IP 白名单） `http://host:port` | ✅ | ✅ |
| 带认证 `http://user:pass@host:port` | ✅ | ❌ 自动跳过，Chrome 走直连 |
| SOCKS5 无认证 `socks5://host:port` | ✅ | ✅ |
| SOCKS5 带认证 `socks5://user:pass@host:port` | ✅ | ❌ 自动跳过，Chrome 走直连 |

> **推荐：** 如果需要 Chrome 渲染也走代理，使用 IP 白名单认证的代理（在代理服务商后台将服务器 IP 加白，去掉用户名密码）。大多数固定 IP 代理服务商都支持此方式。

代码会自动检测 `PROXY_URL` 是否包含认证信息（`@`），有认证时跳过 Chrome 代理注入，确保 Browserless 渲染不会因认证失败而报错。

---

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
