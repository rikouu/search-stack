/**
 * Search Stack — OpenClaw native plugin
 *
 * Registers web search, page fetching, cookie management, and TikHub social
 * media API tools directly into the agent's tool list.  Calls the search-stack
 * REST API over HTTP (same backend as mcp-server.ts) but runs in-process,
 * so timeouts surface as exceptions instead of SIGKILL → zero output.
 */

import { Type } from "@sinclair/typebox";
import type { OpenClawPluginApi } from "openclaw/plugin-sdk";

// =============================================================================
// Types
// =============================================================================

type ApiCallFn = (
  method: string,
  path: string,
  body?: unknown,
  timeoutMs?: number,
) => Promise<unknown>;

// =============================================================================
// Plugin entry
// =============================================================================

export default function register(api: OpenClawPluginApi) {
  const cfg = (api.pluginConfig as {
    apiUrl?: string;
    apiKey?: string;
    tikhubApiKey?: string;
    publicUrl?: string;
  }) ?? {};

  const BASE_URL = cfg.apiUrl || "http://127.0.0.1:17080";
  const API_KEY = cfg.apiKey || "";
  const TIKHUB_API_KEY = cfg.tikhubApiKey || "";
  const PUBLIC_URL = cfg.publicUrl || cfg.apiUrl || "http://127.0.0.1:17080";
  const TIKHUB_URL = "https://mcp.tikhub.io/tools/call";

  // ---------------------------------------------------------------------------
  // HTTP helper
  // ---------------------------------------------------------------------------

  const apiCall: ApiCallFn = async (method, path, body?, timeoutMs = 28_000) => {
    const url = `${BASE_URL}${path}`;
    const headers: Record<string, string> = { "X-API-Key": API_KEY };
    const opts: RequestInit = { method, headers, signal: AbortSignal.timeout(timeoutMs) };
    if (body !== undefined) {
      headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(body);
    }
    const res = await fetch(url, opts);
    if (!res.ok) {
      const text = await res.text().catch(() => "");
      throw new Error(`${method} ${path} → ${res.status}: ${text}`);
    }
    return res.json();
  };

  // ---------------------------------------------------------------------------
  // Tool: web_search
  // ---------------------------------------------------------------------------

  api.registerTool({
    name: "web_search",
    label: "Web Search",
    description:
      "Search the web using multiple engines (Tavily/Serper/SearXNG) with automatic fallback. " +
      "Supports optional full-text enrichment (fetches each result page). " +
      "Best for: general search, research, Chinese platform trending topics.",
    parameters: Type.Object({
      query: Type.String({ description: "Search keywords or question" }),
      count: Type.Optional(
        Type.Number({ description: "Number of results (1-10, default 5)" }),
      ),
      enrich: Type.Optional(
        Type.Boolean({
          description:
            "Fetch full page text for each result (slower but gives complete content)",
        }),
      ),
      max_chars: Type.Optional(
        Type.Number({ description: "Max chars per page when enriching (default 8000)" }),
      ),
      provider: Type.Optional(
        Type.Union(
          [Type.Literal("tavily"), Type.Literal("serper"), Type.Literal("searxng")],
          { description: "Force a specific search provider" },
        ),
      ),
      render: Type.Optional(
        Type.Boolean({
          description:
            "Use headless Chrome when enriching (for anti-bot / JS-heavy sites)",
        }),
      ),
      concurrency: Type.Optional(
        Type.Number({
          description: "Parallel fetches during enrichment (1-8, default 3)",
        }),
      ),
    }),
    async execute(_id: string, params: Record<string, unknown>) {
      const body: Record<string, unknown> = { query: params.query };
      if (params.count !== undefined) body.count = params.count;
      if (params.enrich !== undefined) body.enrich = params.enrich;
      if (params.max_chars !== undefined) body.max_chars = params.max_chars;
      if (params.provider !== undefined) body.provider = params.provider;
      if (params.render !== undefined) body.render = params.render;
      if (params.concurrency !== undefined) body.concurrency = params.concurrency;

      try {
        const data = (await apiCall("POST", "/search", body)) as {
          query: string;
          provider: string;
          results: {
            title: string;
            url: string;
            snippet?: string;
            content?: string;
            fetched?: boolean;
            fetch_error?: string;
          }[];
        };

        const lines: string[] = [];
        lines.push(
          `Search: "${data.query}" (${data.results.length} results via ${data.provider})`,
        );
        lines.push("");

        for (let i = 0; i < data.results.length; i++) {
          const r = data.results[i];
          lines.push(`${i + 1}. ${r.title}`);
          lines.push(`   ${r.url}`);
          if (r.snippet) lines.push(`   ${r.snippet}`);
          if (r.content) {
            lines.push("");
            lines.push(`   --- Page Content ---`);
            lines.push(`   ${r.content}`);
          }
          if (r.fetch_error) lines.push(`   [Fetch error: ${r.fetch_error}]`);
          lines.push("");
        }

        return { content: [{ type: "text" as const, text: lines.join("\n") }] };
      } catch (e) {
        const msg = e instanceof Error ? e.message : String(e);

        // On enrich timeout, retry without enrich to return at least search results
        if (
          params.enrich &&
          (msg.includes("TimeoutError") || msg.includes("abort"))
        ) {
          try {
            const fallbackBody = {
              query: params.query,
              count: (params.count as number) || 5,
              enrich: false,
            };
            const data = (await apiCall("POST", "/search", fallbackBody, 10_000)) as {
              query: string;
              provider: string;
              results: { title: string; url: string; snippet?: string }[];
            };
            const lines: string[] = [];
            lines.push(
              `[Enrich timed out — returning search results without full text]`,
            );
            lines.push(
              `Search: "${data.query}" (${data.results.length} results via ${data.provider})`,
            );
            lines.push("");
            for (let i = 0; i < data.results.length; i++) {
              const r = data.results[i];
              lines.push(`${i + 1}. ${r.title}`);
              lines.push(`   ${r.url}`);
              if (r.snippet) lines.push(`   ${r.snippet}`);
              lines.push("");
            }
            return { content: [{ type: "text" as const, text: lines.join("\n") }] };
          } catch {
            // Even fallback failed — fall through to error
          }
        }

        throw new Error(`Search failed: ${msg}`);
      }
    },
  });

  // ---------------------------------------------------------------------------
  // Tool: web_fetch
  // ---------------------------------------------------------------------------

  api.registerTool({
    name: "web_fetch",
    label: "Fetch Web Page",
    description:
      "Fetch a URL and extract its main text content. Supports headless Chrome rendering " +
      "for anti-bot / JS-heavy pages. When the response contains '** LOGIN REQUIRED **', " +
      "the site needs cookies — send the included login link to the user. " +
      "IMPORTANT: After user logs in via Cookie Catcher, you MUST set bypass_cache=true to skip " +
      "the cached pre-login result and fetch fresh content with the new cookies.",
    parameters: Type.Object({
      url: Type.String({ description: "URL to fetch" }),
      render: Type.Optional(
        Type.Boolean({
          description:
            "Use headless Chrome (default: auto-detect). Set true for anti-bot sites.",
        }),
      ),
      max_chars: Type.Optional(
        Type.Number({ description: "Max text to extract (default 20000)" }),
      ),
      timeout: Type.Optional(
        Type.Number({ description: "Timeout in seconds (default ~25)" }),
      ),
      bypass_cache: Type.Optional(
        Type.Boolean({ description: "Skip cache and fetch fresh. REQUIRED after user logs in or cookies are updated — otherwise you get stale pre-login cached result." }),
      ),
      headers: Type.Optional(
        Type.String({
          description:
            'Custom HTTP headers as JSON string, e.g. \'{"Accept-Language":"ja"}\'',
        }),
      ),
    }),
    async execute(_id: string, params: Record<string, unknown>) {
      const body: Record<string, unknown> = { url: params.url };
      if (params.render !== undefined) body.render = params.render;
      if (params.max_chars !== undefined) body.max_chars = params.max_chars;
      if (params.timeout !== undefined) body.timeout = params.timeout;
      if (params.bypass_cache !== undefined) body.bypass_cache = params.bypass_cache;
      if (params.headers !== undefined) {
        try {
          body.headers = JSON.parse(params.headers as string);
        } catch {
          throw new Error("Invalid headers JSON string");
        }
      }

      const data = (await apiCall("POST", "/fetch", body)) as {
        url: string;
        title?: string;
        text?: string;
        status_code?: number;
        needs_login?: boolean;
        has_cookies?: boolean;
        cached?: boolean;
      };

      const lines: string[] = [];

      if (data.needs_login) {
        lines.push("** LOGIN REQUIRED **");
        lines.push("");
        const domain = new URL(data.url).hostname.replace(/^www\./, "");
        const loginTarget = `https://${domain}`;
        const link = `${PUBLIC_URL}/cookie-catcher?key=${encodeURIComponent(API_KEY)}&url=${encodeURIComponent(loginTarget)}`;

        if (data.has_cookies) {
          lines.push(`${domain} 的 Cookie 已过期，需要重新登录。`);
        } else {
          lines.push(`${domain} 需要登录才能访问完整内容。`);
        }
        lines.push("");
        lines.push(`请在浏览器中打开以下链接登录，Cookie 会自动保存：`);
        lines.push(link);
        lines.push("");
        lines.push(`登录完成后告诉我，我会用 bypass_cache=true 重新抓取。`);
      } else {
        if (data.title) lines.push(`Title: ${data.title}`);
        if (data.url) lines.push(`URL: ${data.url}`);
        if (data.status_code) lines.push(`Status: ${data.status_code}`);
        if (data.cached) lines.push(`(cached)`);
        lines.push("");
        lines.push(data.text || "(no content extracted)");
      }

      return { content: [{ type: "text" as const, text: lines.join("\n") }] };
    },
  });

  // ---------------------------------------------------------------------------
  // Tool: cookies_list
  // ---------------------------------------------------------------------------

  api.registerTool({
    name: "cookies_list",
    label: "List Cookies",
    description:
      "List all domains that have cookies configured. " +
      "Cookies are injected into headless Chrome during render-mode fetches.",
    parameters: Type.Object({}),
    async execute() {
      const data = (await apiCall("GET", "/cookies")) as {
        domains: Record<string, { count: number; names: string[] }>;
      };

      const domains = Object.entries(data.domains);
      if (domains.length === 0) {
        return {
          content: [{ type: "text" as const, text: "No cookies configured for any domain." }],
        };
      }

      const lines: string[] = [`Configured cookies (${domains.length} domains):`];
      for (const [domain, info] of domains) {
        lines.push(`  ${domain}: ${info.count} cookies (${info.names.join(", ")})`);
      }

      return { content: [{ type: "text" as const, text: lines.join("\n") }] };
    },
  });

  // ---------------------------------------------------------------------------
  // Tool: cookies_update
  // ---------------------------------------------------------------------------

  api.registerTool({
    name: "cookies_update",
    label: "Update Cookies",
    description:
      "Save cookies for a domain. Accepts a raw cookie string (name1=val1; name2=val2) " +
      "or a JSON array of cookie objects. Cookies are injected during render-mode fetches.",
    parameters: Type.Object({
      domain: Type.String({ description: 'Target domain (e.g. "xiaohongshu.com")' }),
      raw: Type.Optional(
        Type.String({
          description:
            'Raw cookie string from browser, e.g. "sid=abc; token=xyz". Accepts with or without "Cookie:" prefix.',
        }),
      ),
      cookies: Type.Optional(
        Type.Array(
          Type.Object({
            name: Type.String(),
            value: Type.String(),
            domain: Type.Optional(Type.String()),
            path: Type.Optional(Type.String()),
          }),
        ),
      ),
    }),
    async execute(_id: string, params: Record<string, unknown>) {
      const body: Record<string, unknown> = {};
      if (params.raw) body.raw = params.raw;
      if (params.cookies) body.cookies = params.cookies;

      const data = (await apiCall(
        "PUT",
        `/cookies/${encodeURIComponent(params.domain as string)}`,
        body,
      )) as {
        ok: boolean;
        domain: string;
        count: number;
        names: string[];
      };

      return {
        content: [
          {
            type: "text" as const,
            text: `Saved ${data.count} cookies for ${data.domain}: ${data.names.join(", ")}`,
          },
        ],
      };
    },
  });

  // ---------------------------------------------------------------------------
  // Tool: cookies_delete
  // ---------------------------------------------------------------------------

  api.registerTool({
    name: "cookies_delete",
    label: "Delete Cookies",
    description: "Delete all cookies for a domain.",
    parameters: Type.Object({
      domain: Type.String({
        description: 'Domain to delete cookies for (e.g. "xiaohongshu.com")',
      }),
    }),
    async execute(_id: string, params: Record<string, unknown>) {
      const data = (await apiCall(
        "DELETE",
        `/cookies/${encodeURIComponent(params.domain as string)}`,
      )) as {
        ok: boolean;
        domain: string;
      };

      return {
        content: [
          { type: "text" as const, text: `Deleted cookies for ${data.domain}.` },
        ],
      };
    },
  });

  // ---------------------------------------------------------------------------
  // Tool: cookie_catcher_link
  // ---------------------------------------------------------------------------

  api.registerTool({
    name: "cookie_catcher_link",
    label: "Cookie Login Link",
    description:
      "Generate a remote browser login link. The user opens it in their browser, " +
      "logs into the target site, and cookies are automatically captured and saved. " +
      "Use when: web_fetch returns LOGIN REQUIRED, user asks to add cookies, " +
      "or you need login cookies for a site.",
    parameters: Type.Object({
      url: Type.Optional(Type.String({
        description: "Target URL to open (e.g. https://youtube.com). Auto-navigates on open.",
      })),
    }),
    async execute(_id: string, params: Record<string, unknown>) {
      const targetUrl = (params.url as string) || "";
      let link = `${PUBLIC_URL}/cookie-catcher?key=${encodeURIComponent(API_KEY)}`;
      if (targetUrl) link += `&url=${encodeURIComponent(targetUrl)}`;
      return {
        content: [{
          type: "text" as const,
          text: `请在浏览器中打开以下链接登录，Cookie 会自动保存：\n${link}\n\n登录完成后告诉我，我会重新抓取。`,
        }],
      };
    },
  });

  // ---------------------------------------------------------------------------
  // Tool: tikhub_call (conditional — only if API key configured)
  // ---------------------------------------------------------------------------

  if (TIKHUB_API_KEY) {
    // Platform name mapping for fallback search queries
    const PLATFORM_NAMES: Record<string, string> = {
      douyin: "抖音",
      tiktok: "TikTok",
      xiaohongshu: "小红书",
      weibo: "微博",
      bilibili: "B站",
      instagram: "Instagram",
      youtube: "YouTube",
      twitter: "Twitter",
      kuaishou: "快手",
      reddit: "Reddit",
    };

    function extractPlatform(toolName: string): string | undefined {
      const prefix = toolName.split("_")[0];
      return PLATFORM_NAMES[prefix];
    }

    function extractKeyword(args: Record<string, unknown>): string | undefined {
      for (const key of ["keyword", "query", "search_keyword", "keywords"]) {
        if (typeof args[key] === "string" && args[key]) return args[key] as string;
      }
      return undefined;
    }

    function isEmptyData(data: unknown): boolean {
      if (data === null || data === undefined) return true;
      if (typeof data === "object") {
        if (Array.isArray(data)) return data.length === 0;
        const obj = data as Record<string, unknown>;
        const keys = Object.keys(obj);
        if (keys.length === 0) return true;
        if ("data" in obj && isEmptyData(obj.data)) {
          const meaningful = keys.filter(
            (k) => !["code", "data", "message", "recordTime", "msg"].includes(k),
          );
          if (meaningful.length === 0) return true;
        }
        for (const k of [
          "items",
          "item_list",
          "notes",
          "note_list",
          "list",
          "results",
          "video_list",
        ]) {
          if (
            k in obj &&
            Array.isArray(obj[k]) &&
            (obj[k] as unknown[]).length === 0
          )
            return true;
        }
      }
      return false;
    }

    async function fallbackWebSearch(
      keyword: string,
      platform?: string,
    ): Promise<string | null> {
      const query = platform ? `${platform} ${keyword}` : keyword;
      try {
        const data = (await apiCall("POST", "/search", {
          query,
          count: 5,
          enrich: true,
          max_chars: 5000,
        })) as {
          query: string;
          provider: string;
          results: {
            title: string;
            url: string;
            snippet?: string;
            content?: string;
          }[];
        };

        if (!data.results || data.results.length === 0) return null;

        const lines: string[] = [];
        lines.push(`[TikHub 不可用，已自动回退到网页搜索]`);
        lines.push(
          `Search: "${data.query}" (${data.results.length} results via ${data.provider})`,
        );
        lines.push("");

        for (let i = 0; i < data.results.length; i++) {
          const r = data.results[i];
          lines.push(`${i + 1}. ${r.title}`);
          lines.push(`   ${r.url}`);
          if (r.snippet) lines.push(`   ${r.snippet}`);
          if (r.content) {
            lines.push("");
            lines.push(`   --- Page Content ---`);
            lines.push(`   ${r.content}`);
          }
          lines.push("");
        }

        return lines.join("\n");
      } catch {
        return null;
      }
    }

    api.registerTool({
      name: "tikhub_call",
      label: "TikHub Social Media API",
      description:
        "LAST RESORT: Third-party social media API. " +
        "ONLY use AFTER: (1) web_search tried, (2) web_fetch tried, (3) login link sent to user, " +
        "(4) user explicitly declined to login or said 'use backup'. " +
        "NEVER call this as the first step. Always try own tools first. " +
        "Covers: Douyin, TikTok, Xiaohongshu, Weibo, Bilibili, Instagram, YouTube, Twitter/X. " +
        "Has automatic fallback: if TikHub fails and the request has a keyword, " +
        "it falls back to web search automatically. " +
        "Common tool names (use EXACTLY these, do NOT guess): " +
        "xiaohongshu_web_search_notes(keyword), xiaohongshu_app_search_notes(keyword,page), " +
        "xiaohongshu_app_fetch_note_info(note_id), " +
        "tiktok_web_fetch_search_video(keyword,count), " +
        "douyin_app_fetch_hot_search_list(), " +
        "weibo_web_v2_fetch_hot_search_summary(), " +
        "bilibili_web_fetch_search_result(keyword,page), " +
        "youtube_web_fetch_search_result(keyword).",
      parameters: Type.Object({
        tool_name: Type.String({
          description:
            "TikHub tool name — use EXACT names from the tool description above. " +
            "Do NOT guess or fabricate tool names (e.g. 'xiaohongshu_web_fetch_search_notes' does NOT exist, " +
            "the correct name is 'xiaohongshu_web_search_notes').",
        }),
        arguments: Type.Optional(
          Type.Unknown({
            description:
              'Tool arguments as object or JSON string, e.g. {"keyword":"AI","count":3}',
          }),
        ),
      }),
      async execute(_id: string, params: Record<string, unknown>) {
        // Handle both string and pre-parsed object
        let args: Record<string, unknown>;
        const rawArgs = params.arguments;
        if (typeof rawArgs === "object" && rawArgs !== null) {
          args = rawArgs as Record<string, unknown>;
        } else {
          try {
            args = JSON.parse((rawArgs as string) || "{}");
          } catch {
            throw new Error("Invalid arguments JSON string");
          }
        }

        const toolName = params.tool_name as string;
        const keyword = extractKeyword(args);
        const platform = extractPlatform(toolName);

        // --- Try TikHub ---
        let tikhubFailed = false;
        let failReason = "";

        try {
          const res = await fetch(TIKHUB_URL, {
            method: "POST",
            headers: {
              Authorization: `Bearer ${TIKHUB_API_KEY}`,
              "Content-Type": "application/json",
            },
            body: JSON.stringify({ tool_name: toolName, arguments: args }),
            signal: AbortSignal.timeout(25000),
          });

          if (!res.ok) {
            const text = await res.text().catch(() => "");
            tikhubFailed = true;
            failReason = `HTTP ${res.status}: ${text.slice(0, 200)}`;
          } else {
            const data = (await res.json()) as {
              result?: unknown;
              error?: string;
            };

            if (data.error) {
              tikhubFailed = true;
              failReason = data.error;
            } else {
              const result = data.result as Record<string, unknown> | undefined;
              if (result && typeof result === "object") {
                const innerData = result.data as unknown;
                const code = result.code as number | undefined;
                const message = result.message as string | undefined;

                if (code && code !== 200) {
                  tikhubFailed = true;
                  failReason = `${toolName} returned code ${code}: ${message || "error"}`;
                } else if (isEmptyData(innerData)) {
                  tikhubFailed = true;
                  failReason = `${toolName} returned empty data (platform anti-bot)`;
                } else {
                  let text = JSON.stringify(innerData ?? result, null, 2);
                  if (text.length > 50000) {
                    text =
                      text.slice(0, 50000) + "\n\n... (truncated, result too large)";
                  }
                  return {
                    content: [
                      {
                        type: "text" as const,
                        text: `TikHub ${toolName} result:\n\n${text}`,
                      },
                    ],
                  };
                }
              } else {
                let text = JSON.stringify(data, null, 2);
                if (text.length > 50000) {
                  text = text.slice(0, 50000) + "\n\n... (truncated)";
                }
                return { content: [{ type: "text" as const, text }] };
              }
            }
          }
        } catch (e) {
          tikhubFailed = true;
          failReason = e instanceof Error ? e.message : String(e);
        }

        // --- TikHub failed: try web search fallback ---
        if (tikhubFailed && keyword) {
          const fallbackResult = await fallbackWebSearch(keyword, platform);
          if (fallbackResult) {
            return {
              content: [
                {
                  type: "text" as const,
                  text: `TikHub 失败 (${failReason})，已自动回退到网页搜索:\n\n${fallbackResult}`,
                },
              ],
            };
          }
        }

        // Both failed or no keyword to fall back with
        const errorMsg = keyword
          ? `TikHub 失败: ${failReason}\n网页搜索回退也失败了。`
          : `TikHub 失败: ${failReason}\n该工具没有 keyword 参数，无法自动回退到网页搜索。`;

        throw new Error(errorMsg);
      },
    });
  }
}
