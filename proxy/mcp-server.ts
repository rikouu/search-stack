#!/usr/bin/env bun
/**
 * Search Stack MCP Server — stdio wrapper for search-stack REST API
 *
 * Exposes web search, page fetching, and cookie management as MCP tools.
 * Replaces built-in Brave web_search and curl-based skill workflow.
 *
 * Follows MCP spec 2025-06-18, same pattern as qmd.
 */

import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { z } from "zod";

// =============================================================================
// Configuration
// =============================================================================

const BASE_URL = process.env.SEARCH_STACK_URL || "http://127.0.0.1:17080";
const API_KEY = process.env.SEARCH_STACK_API_KEY || "";
const TIKHUB_API_KEY = process.env.TIKHUB_API_KEY || "";
const TIKHUB_URL = "https://mcp.tikhub.io/tools/call";

// =============================================================================
// HTTP helpers
// =============================================================================

async function api(
  method: string,
  path: string,
  body?: unknown,
): Promise<unknown> {
  const url = `${BASE_URL}${path}`;
  const headers: Record<string, string> = {
    "X-API-Key": API_KEY,
  };
  const opts: RequestInit = { method, headers };
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
}

// =============================================================================
// MCP Server
// =============================================================================

const server = new McpServer({
  name: "search-stack",
  version: "1.0.0",
});

// ---------------------------------------------------------------------------
// Tool: web_search — POST /search
// ---------------------------------------------------------------------------

server.registerTool(
  "web_search",
  {
    title: "Web Search",
    description:
      "Search the web using multiple engines (Tavily/Serper/SearXNG) with automatic fallback. " +
      "Supports optional full-text enrichment (fetches each result page). " +
      "Best for: general search, research, Chinese platform trending topics.",
    inputSchema: {
      query: z.string().describe("Search keywords or question"),
      count: z
        .number()
        .min(1)
        .max(10)
        .optional()
        .default(5)
        .describe("Number of results (1-10, default 5)"),
      enrich: z
        .boolean()
        .optional()
        .default(false)
        .describe(
          "Fetch full page text for each result (slower but gives complete content)",
        ),
      max_chars: z
        .number()
        .optional()
        .default(8000)
        .describe("Max chars per page when enriching (default 8000)"),
      provider: z
        .enum(["tavily", "serper", "searxng"])
        .optional()
        .describe("Force a specific search provider"),
      render: z
        .boolean()
        .optional()
        .describe(
          "Use headless Chrome when enriching (for anti-bot / JS-heavy sites)",
        ),
      concurrency: z
        .number()
        .min(1)
        .max(8)
        .optional()
        .describe("Parallel fetches during enrichment (default 3)"),
    },
  },
  async (params) => {
    const body: Record<string, unknown> = { query: params.query };
    if (params.count !== undefined) body.count = params.count;
    if (params.enrich !== undefined) body.enrich = params.enrich;
    if (params.max_chars !== undefined) body.max_chars = params.max_chars;
    if (params.provider !== undefined) body.provider = params.provider;
    if (params.render !== undefined) body.render = params.render;
    if (params.concurrency !== undefined) body.concurrency = params.concurrency;

    const data = (await api("POST", "/search", body)) as {
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

    // Format as readable text
    const lines: string[] = [];
    lines.push(
      `Search: "${data.query}" (${data.results.length} results via ${data.provider})`,
    );
    lines.push("");

    for (let i = 0; i < data.results.length; i++) {
      const r = data.results[i];
      lines.push(`${i + 1}. ${r.title}`);
      lines.push(`   ${r.url}`);
      if (r.snippet) {
        lines.push(`   ${r.snippet}`);
      }
      if (r.content) {
        lines.push("");
        lines.push(`   --- Page Content ---`);
        lines.push(`   ${r.content}`);
      }
      if (r.fetch_error) {
        lines.push(`   [Fetch error: ${r.fetch_error}]`);
      }
      lines.push("");
    }

    return {
      content: [{ type: "text", text: lines.join("\n") }],
    };
  },
);

// ---------------------------------------------------------------------------
// Tool: web_fetch — POST /fetch
// ---------------------------------------------------------------------------

server.registerTool(
  "web_fetch",
  {
    title: "Fetch Web Page",
    description:
      "Fetch a URL and extract its main text content. Supports headless Chrome rendering " +
      "for anti-bot / JS-heavy pages. When the response contains '** LOGIN REQUIRED **', " +
      "the site needs cookies — use cookies_update to provide them, then retry with bypass_cache.",
    inputSchema: {
      url: z.string().describe("URL to fetch"),
      render: z
        .boolean()
        .optional()
        .describe(
          "Use headless Chrome (default: auto-detect). Set true for anti-bot sites.",
        ),
      max_chars: z
        .number()
        .optional()
        .default(20000)
        .describe("Max text to extract (default 20000)"),
      timeout: z
        .number()
        .optional()
        .describe("Timeout in seconds (default ~25)"),
      bypass_cache: z
        .boolean()
        .optional()
        .default(false)
        .describe("Skip cache — use after updating cookies"),
      headers: z
        .string()
        .optional()
        .describe('Custom HTTP headers as JSON string, e.g. \'{"Accept-Language":"ja"}\''),
    },
  },
  async (params) => {
    const body: Record<string, unknown> = { url: params.url };
    if (params.render !== undefined) body.render = params.render;
    if (params.max_chars !== undefined) body.max_chars = params.max_chars;
    if (params.timeout !== undefined) body.timeout = params.timeout;
    if (params.bypass_cache !== undefined)
      body.bypass_cache = params.bypass_cache;
    if (params.headers !== undefined) {
      try {
        body.headers = JSON.parse(params.headers);
      } catch {
        return { content: [{ type: "text", text: "Invalid headers JSON string" }], isError: true };
      }
    }

    const data = (await api("POST", "/fetch", body)) as {
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
      if (data.has_cookies) {
        lines.push(
          `The cookies for ${domain} appear to have expired. Please paste fresh cookies from your browser.`,
        );
      } else {
        lines.push(
          `This site requires login. Please paste your browser cookies for ${domain}.`,
        );
      }
      lines.push(
        `(DevTools → Application → Cookies, or copy the Cookie header value)`,
      );
      lines.push("");
      lines.push(
        `After saving cookies with cookies_update, retry this fetch with bypass_cache=true.`,
      );
    } else {
      if (data.title) lines.push(`Title: ${data.title}`);
      if (data.url) lines.push(`URL: ${data.url}`);
      if (data.status_code) lines.push(`Status: ${data.status_code}`);
      if (data.cached) lines.push(`(cached)`);
      lines.push("");
      lines.push(data.text || "(no content extracted)");
    }

    return {
      content: [{ type: "text", text: lines.join("\n") }],
    };
  },
);

// ---------------------------------------------------------------------------
// Tool: cookies_list — GET /cookies
// ---------------------------------------------------------------------------

server.registerTool(
  "cookies_list",
  {
    title: "List Cookies",
    description:
      "List all domains that have cookies configured. " +
      "Cookies are injected into headless Chrome during render-mode fetches.",
    inputSchema: {},
  },
  async () => {
    const data = (await api("GET", "/cookies")) as {
      domains: Record<string, { count: number; names: string[] }>;
    };

    const domains = Object.entries(data.domains);
    if (domains.length === 0) {
      return {
        content: [
          { type: "text", text: "No cookies configured for any domain." },
        ],
      };
    }

    const lines: string[] = [`Configured cookies (${domains.length} domains):`];
    for (const [domain, info] of domains) {
      lines.push(`  ${domain}: ${info.count} cookies (${info.names.join(", ")})`);
    }

    return {
      content: [{ type: "text", text: lines.join("\n") }],
    };
  },
);

// ---------------------------------------------------------------------------
// Tool: cookies_update — PUT /cookies/{domain}
// ---------------------------------------------------------------------------

server.registerTool(
  "cookies_update",
  {
    title: "Update Cookies",
    description:
      "Save cookies for a domain. Accepts a raw cookie string (name1=val1; name2=val2) " +
      "or a JSON array of cookie objects. Cookies are injected during render-mode fetches.",
    inputSchema: {
      domain: z
        .string()
        .describe('Target domain (e.g. "xiaohongshu.com")'),
      raw: z
        .string()
        .optional()
        .describe(
          'Raw cookie string from browser, e.g. "sid=abc; token=xyz". Accepts with or without "Cookie:" prefix.',
        ),
      cookies: z
        .array(
          z.object({
            name: z.string(),
            value: z.string(),
            domain: z.string().optional(),
            path: z.string().optional(),
          }),
        )
        .optional()
        .describe("JSON array of cookie objects (alternative to raw)"),
    },
  },
  async (params) => {
    const body: Record<string, unknown> = {};
    if (params.raw) body.raw = params.raw;
    if (params.cookies) body.cookies = params.cookies;

    const data = (await api(
      "PUT",
      `/cookies/${encodeURIComponent(params.domain)}`,
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
          type: "text",
          text: `Saved ${data.count} cookies for ${data.domain}: ${data.names.join(", ")}`,
        },
      ],
    };
  },
);

// ---------------------------------------------------------------------------
// Tool: cookies_delete — DELETE /cookies/{domain}
// ---------------------------------------------------------------------------

server.registerTool(
  "cookies_delete",
  {
    title: "Delete Cookies",
    description: "Delete all cookies for a domain.",
    inputSchema: {
      domain: z
        .string()
        .describe('Domain to delete cookies for (e.g. "xiaohongshu.com")'),
    },
  },
  async (params) => {
    const data = (await api(
      "DELETE",
      `/cookies/${encodeURIComponent(params.domain)}`,
    )) as {
      ok: boolean;
      domain: string;
    };

    return {
      content: [
        {
          type: "text",
          text: `Deleted cookies for ${data.domain}.`,
        },
      ],
    };
  },
);

// ---------------------------------------------------------------------------
// Tool: tikhub_call — proxy to TikHub social media MCP API (with fallback)
// ---------------------------------------------------------------------------

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
  // TikHub tools use "keyword", "query", "search_keyword", or "keywords" for search terms
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
    // Directly empty
    if (keys.length === 0) return true;
    // TikHub nested pattern: { code: 0, data: {}, message: null, recordTime: "..." }
    // The actual payload is in .data — if that's empty, the result is useless
    if ("data" in obj && isEmptyData(obj.data)) {
      // Check if there's any other meaningful field besides metadata
      const meaningful = keys.filter(k => !["code", "data", "message", "recordTime", "msg"].includes(k));
      if (meaningful.length === 0) return true;
    }
    // items/item_list pattern: empty array
    for (const k of ["items", "item_list", "notes", "note_list", "list", "results", "video_list"]) {
      if (k in obj && Array.isArray(obj[k]) && (obj[k] as unknown[]).length === 0) return true;
    }
  }
  return false;
}

async function fallbackWebSearch(keyword: string, platform?: string): Promise<string | null> {
  const query = platform ? `${platform} ${keyword}` : keyword;
  try {
    const data = (await api("POST", "/search", {
      query,
      count: 5,
      enrich: true,
      max_chars: 5000,
    })) as {
      query: string;
      provider: string;
      results: { title: string; url: string; snippet?: string; content?: string }[];
    };

    if (!data.results || data.results.length === 0) return null;

    const lines: string[] = [];
    lines.push(`[TikHub 不可用，已自动回退到网页搜索]`);
    lines.push(`Search: "${data.query}" (${data.results.length} results via ${data.provider})`);
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

server.registerTool(
  "tikhub_call",
  {
    title: "TikHub Social Media API",
    description:
      "Call any TikHub social media API tool (803 tools across Douyin, TikTok, Xiaohongshu, " +
      "Weibo, Bilibili, Instagram, YouTube, Twitter/X, etc.). " +
      "Use this for social media platform data that search-stack can't scrape due to anti-bot. " +
      "Common tools: tiktok_web_fetch_search_video, douyin_app_fetch_hot_search_list, " +
      "xiaohongshu_app_get_note_info, weibo_web_v2_fetch_hot_search_summary. " +
      "Has automatic fallback: if TikHub fails and the request has a keyword, " +
      "it falls back to web search automatically.",
    inputSchema: {
      tool_name: z.string().describe(
        "TikHub tool name, e.g. 'tiktok_web_fetch_search_video', 'douyin_app_fetch_hot_search_list'"
      ),
      arguments: z.any().optional().describe(
        'Tool arguments as object or JSON string, e.g. {"keyword":"AI","count":3}'
      ),
    },
  },
  async (params) => {
    // Handle both string and pre-parsed object (mcporter auto-parses JSON strings)
    let args: Record<string, unknown>;
    const rawArgs = params.arguments as unknown;
    if (typeof rawArgs === "object" && rawArgs !== null) {
      args = rawArgs as Record<string, unknown>;
    } else {
      try {
        args = JSON.parse((rawArgs as string) || "{}");
      } catch {
        return {
          content: [{ type: "text", text: "Invalid arguments JSON string" }],
          isError: true,
        };
      }
    }

    const keyword = extractKeyword(args);
    const platform = extractPlatform(params.tool_name);

    // --- Try TikHub ---
    let tikhubFailed = false;
    let failReason = "";

    if (!TIKHUB_API_KEY) {
      tikhubFailed = true;
      failReason = "API key not configured";
    }

    if (!tikhubFailed) {
      try {
        const res = await fetch(TIKHUB_URL, {
          method: "POST",
          headers: {
            "Authorization": `Bearer ${TIKHUB_API_KEY}`,
            "Content-Type": "application/json",
          },
          body: JSON.stringify({
            tool_name: params.tool_name,
            arguments: args,
          }),
          signal: AbortSignal.timeout(25000),
        });

        if (!res.ok) {
          const text = await res.text().catch(() => "");
          tikhubFailed = true;
          failReason = `HTTP ${res.status}: ${text.slice(0, 200)}`;
        } else {
          const data = await res.json() as {
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

              // Non-200 code = upstream error
              if (code && code !== 200) {
                tikhubFailed = true;
                failReason = `${params.tool_name} returned code ${code}: ${message || "error"}`;
              }
              // 200 but empty data = platform anti-bot
              else if (isEmptyData(innerData)) {
                tikhubFailed = true;
                failReason = `${params.tool_name} returned empty data (platform anti-bot)`;
              }
              // Success!
              else {
                let text = JSON.stringify(innerData ?? result, null, 2);
                if (text.length > 50000) {
                  text = text.slice(0, 50000) + "\n\n... (truncated, result too large)";
                }
                return {
                  content: [{ type: "text", text: `TikHub ${params.tool_name} result:\n\n${text}` }],
                };
              }
            } else {
              // No structured result
              let text = JSON.stringify(data, null, 2);
              if (text.length > 50000) {
                text = text.slice(0, 50000) + "\n\n... (truncated)";
              }
              return {
                content: [{ type: "text", text }],
              };
            }
          }
        }
      } catch (e) {
        tikhubFailed = true;
        failReason = e instanceof Error ? e.message : String(e);
      }
    }

    // --- TikHub failed: try fallback ---
    if (tikhubFailed && keyword) {
      const fallbackResult = await fallbackWebSearch(keyword, platform);
      if (fallbackResult) {
        return {
          content: [{
            type: "text",
            text: `TikHub 失败 (${failReason})，已自动回退到网页搜索:\n\n${fallbackResult}`,
          }],
        };
      }
    }

    // Both failed or no keyword to fall back with
    const errorMsg = tikhubFailed
      ? `TikHub 失败: ${failReason}` + (keyword ? "\n网页搜索回退也失败了。" : "\n该工具没有 keyword 参数，无法自动回退到网页搜索。")
      : "Unknown error";

    return {
      content: [{ type: "text", text: errorMsg }],
      isError: true,
    };
  },
);

// =============================================================================
// Start
// =============================================================================

const transport = new StdioServerTransport();
await server.connect(transport);
