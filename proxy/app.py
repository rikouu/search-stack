import os
import time
import json
import hashlib
import re
import socket
import logging
import ipaddress
from typing import Dict, Any, List, Optional, Tuple
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode
from contextlib import asynccontextmanager

import httpx
import redis.asyncio as aioredis
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from bs4 import BeautifulSoup
import trafilatura

# ===================== Logging =====================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("search-proxy")

# ===================== ENV =====================
TAVILY_API_KEY = os.getenv("TAVILY_API_KEY", "").strip()
SERPER_API_KEY = os.getenv("SERPER_API_KEY", "").strip()
SEARXNG_URL = os.getenv("SEARXNG_URL", "http://searxng:8080").rstrip("/")

REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0").strip()
CACHE_TTL = int(os.getenv("CACHE_TTL", "900"))
ORDER = [x.strip() for x in os.getenv("ORDER", "tavily,serper,searxng").split(",") if x.strip()]

API_KEYS = [x.strip() for x in os.getenv("API_KEYS", "").split(",") if x.strip()]
RATE_LIMIT_PER_MIN = int(os.getenv("RATE_LIMIT_PER_MIN", "60"))

BROWSERLESS_HTTP = os.getenv("BROWSERLESS_HTTP", "http://browserless:3000").rstrip("/")
BROWSERLESS_TOKEN = os.getenv("BROWSERLESS_TOKEN", "").strip()
FETCH_TIMEOUT = float(os.getenv("FETCH_TIMEOUT", "25"))
ENRICH_PER_PAGE_TIMEOUT = float(os.getenv("ENRICH_PER_PAGE_TIMEOUT", "10"))  # per-page timeout during enrich (shorter)
ENRICH_WALL_TIMEOUT = float(os.getenv("ENRICH_WALL_TIMEOUT", "20"))  # total wall-clock for all enrich fetches
MAX_FETCH_BYTES = int(os.getenv("MAX_FETCH_BYTES", "2000000"))  # 2MB default

FETCH_USER_AGENT = os.getenv(
    "FETCH_USER_AGENT",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
).strip()

FETCH_DEFAULT_RENDER = os.getenv("FETCH_DEFAULT_RENDER", "true").lower() in ("1", "true", "yes", "y")

# Cookie 注入（Browserless）
COOKIES_FILE = os.getenv("COOKIES_FILE", "/app/cookies.json")
_domain_cookies: Dict[str, List[Dict[str, Any]]] = {}


def load_cookies() -> int:
    """从 cookies.json 加载域名 Cookie 映射，返回加载的域名数。"""
    global _domain_cookies
    try:
        with open(COOKIES_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except FileNotFoundError:
        log.info("cookies file not found: %s", COOKIES_FILE)
        _domain_cookies = {}
        return 0
    except Exception as e:
        log.warning("failed to load cookies: %s", e)
        _domain_cookies = {}
        return 0

    loaded = {}
    for domain, cookies in raw.items():
        if domain.startswith("_"):
            continue
        if isinstance(cookies, list) and len(cookies) > 0:
            loaded[domain.lower().strip(".")] = cookies
    _domain_cookies = loaded
    log.info("loaded cookies for %d domains: %s", len(loaded), list(loaded.keys()))
    return len(loaded)


def save_cookies() -> None:
    """将内存中的 _domain_cookies 写回 cookies.json。"""
    data = {"_doc": "按域名存放 Cookie，Browserless 会在加载页面前注入。"}
    data.update(_domain_cookies)
    try:
        with open(COOKIES_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        log.info("saved cookies for %d domains to %s", len(_domain_cookies), COOKIES_FILE)
    except Exception as e:
        log.error("failed to save cookies: %s", e)
        raise


def parse_raw_cookie_string(raw: str, domain: str) -> List[Dict[str, Any]]:
    """解析 'name1=val1; name2=val2' 格式的 cookie 字符串，支持 'Cookie: ' 前缀。"""
    raw = raw.strip()
    if raw.lower().startswith("cookie:"):
        raw = raw[7:].strip()
    cookies = []
    for pair in raw.split(";"):
        pair = pair.strip()
        if not pair or "=" not in pair:
            continue
        name, _, value = pair.partition("=")
        name = name.strip()
        value = value.strip()
        if not name:
            continue
        cookies.append({
            "name": name,
            "value": value,
            "domain": f".{domain.lstrip('.')}",
            "path": "/",
        })
    return cookies


def detect_needs_login(text: str, url: str, *,
                       html: str = "",
                       status_code: int = 200,
                       title: str = "") -> bool:
    """启发式检测页面是否需要登录/反爬拦截（HTTP状态码 + 关键词 + HTML结构 + 标题）。"""
    if not text:
        return False
    text_lower = text.lower()
    stripped = text.strip()

    # 规则 0：HTTP 状态码
    if status_code == 401:
        return True
    if status_code == 403 and len(stripped) < 2000:
        return True

    # 规则 1：明确的登录关键词
    login_keywords = [
        "请登录", "请先登录", "需要登录", "登录后", "登录才能",
        "未登录", "请注册", "登录/注册",
        "please log in", "please sign in", "login required",
        "sign in to continue", "log in to continue",
        "you need to log in", "you must be logged in",
        "authentication required",
        # Social media / SPA login walls
        "log in with your instagram",
        "log in with your facebook",
        "forgot password?",
        # OAuth / social login prompts
        "sign in with", "continue with google", "continue with apple",
        "continue with facebook", "create an account",
        "sign up to continue", "join to continue",
        "log in to see", "sign in to see",
        # CAPTCHA / verification
        "verify you are human", "complete the captcha",
        "security verification", "please verify",
        "checking your browser", "just a moment",
        # Japanese
        "ログインしてください", "ログインが必要",
        # Paywalls (often require login/signup)
        "subscribe to continue", "members only",
        "premium content", "subscriber-only",
    ]
    keyword_hits = sum(1 for kw in login_keywords if kw in text_lower)
    if keyword_hits >= 1 and len(stripped) < 500:
        return True
    if keyword_hits >= 2:
        return True

    # 规则 2：页面标题检测
    if title:
        title_lower = title.lower()
        title_login_kw = ["sign in", "log in", "login", "登录", "ログイン", "登入"]
        if any(kw in title_lower for kw in title_login_kw) and len(stripped) < 2000:
            return True

    # 规则 3：HTML 结构检测
    if html:
        html_lower = html[:50000].lower()

        # 3a: 密码输入框 = 登录表单的直接证据
        if ('type="password"' in html_lower or "type='password'" in html_lower) and len(stripped) < 3000:
            return True

        # 3b: Meta refresh 重定向到登录相关 URL
        if 'http-equiv="refresh"' in html_lower or "http-equiv='refresh'" in html_lower:
            refresh_login_kw = ["/login", "/signin", "/auth", "/sso", "login."]
            if any(kw in html_lower for kw in refresh_login_kw):
                return True

        # 3c: CAPTCHA 嵌入
        captcha_sigs = ["recaptcha", "hcaptcha", "challenges.cloudflare.com", "turnstile"]
        if any(s in html_lower for s in captcha_sigs) and len(stripped) < 1000:
            return True

    # 规则 4：空壳页面 — 内容很少且全是备案/页脚信息（如小红书搜索页未登录）
    boilerplate_keywords = [
        "icp备", "icp证", "沪icp", "京icp", "粤icp", "浙icp",
        "营业执照", "公网安备", "增值电信", "网络文化经营许可",
        "违法不良信息举报", "互联网药品信息", "网械平台备字",
    ]
    boilerplate_hits = sum(1 for kw in boilerplate_keywords if kw in text_lower)
    if boilerplate_hits >= 2 and len(stripped) < 800:
        return True

    return False


def get_cookies_for_url(url: str) -> List[Dict[str, Any]]:
    """根据 URL 域名匹配 cookie 配置。"""
    host = host_of(url).lower().strip(".")
    if not host:
        return []
    for domain, cookies in _domain_cookies.items():
        if host == domain or host.endswith("." + domain):
            return cookies
    return []


# SSRF / 域名策略
ALLOW_DOMAINS = [x.strip().lower() for x in os.getenv("ALLOW_DOMAINS", "").split(",") if x.strip()]
BLOCK_DOMAINS = [x.strip().lower() for x in os.getenv("BLOCK_DOMAINS", "").split(",") if x.strip()]
# 永久黑名单（云 metadata / 本机）
BLOCK_IP_CIDRS = [
    "127.0.0.0/8",     # loopback
    "10.0.0.0/8",      # private
    "172.16.0.0/12",   # private
    "192.168.0.0/16",  # private
    "169.254.0.0/16",  # link-local
    "0.0.0.0/8",
    "100.64.0.0/10",   # carrier-grade NAT
    "224.0.0.0/4",     # multicast
    "240.0.0.0/4",     # reserved
    "fe80::/10",       # IPv6 link-local
    "::1/128",         # IPv6 loopback
    "fc00::/7",        # IPv6 unique local
]
BLOCK_IP_NETWORKS = [ipaddress.ip_network(x) for x in BLOCK_IP_CIDRS]

# 去重 / 聚类
DEDUP = os.getenv("DEDUPE", "true").lower() in ("1", "true", "yes", "y")
MAX_PER_HOST = int(os.getenv("MAX_PER_HOST", "2"))

# ===================== Global clients (set in lifespan) =====================
rds: aioredis.Redis = None  # type: ignore
http_client: httpx.AsyncClient = None  # type: ignore


@asynccontextmanager
async def lifespan(application: FastAPI):
    global rds, http_client
    rds = aioredis.from_url(REDIS_URL, decode_responses=True)
    http_client = httpx.AsyncClient(
        timeout=30,
        follow_redirects=True,
        headers={"User-Agent": FETCH_USER_AGENT},
        limits=httpx.Limits(max_connections=50, max_keepalive_connections=20),
    )
    load_cookies()
    log.info("search-proxy started, providers=%s", ORDER)
    yield
    await http_client.aclose()
    await rds.aclose()
    log.info("search-proxy stopped")


app = FastAPI(title="Search Proxy", version="3.2", lifespan=lifespan)

# ===================== Helpers =====================
def sha(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def now_min_bucket() -> str:
    return str(int(time.time() // 60))


def get_api_key(req: Request) -> Optional[str]:
    return (req.headers.get("X-API-Key") or req.query_params.get("key") or "").strip() or None


async def enforce_auth_and_ratelimit(req: Request) -> str:
    if API_KEYS:
        k = get_api_key(req)
        if not k or k not in API_KEYS:
            raise HTTPException(401, "Unauthorized: missing/invalid API key (use X-API-Key)")
        key_id = k
    else:
        key_id = (req.client.host if req.client else "unknown")

    bucket = now_min_bucket()
    rl_key = f"rl:{key_id}:{bucket}"
    n = await rds.incr(rl_key)
    if n == 1:
        await rds.expire(rl_key, 70)
    if n > RATE_LIMIT_PER_MIN:
        raise HTTPException(429, f"Rate limit exceeded: {RATE_LIMIT_PER_MIN}/min")

    return key_id


def normalize(title: str, url: str, snippet: str, source: str) -> Dict[str, Any]:
    return {"title": title or "", "url": url or "", "snippet": snippet or "", "source": source}


def is_http_url(u: str) -> bool:
    try:
        p = urlparse(u)
        return p.scheme in ("http", "https") and bool(p.netloc)
    except Exception:
        return False


def truncate_text(s: str, limit: int) -> str:
    s = s or ""
    return s if len(s) <= limit else s[:limit] + "…"


def strip_junk_whitespace(s: str) -> str:
    s = re.sub(r"\r", "", s or "")
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


# ---------- URL Canonicalization / Dedupe ----------
TRACKING_KEYS_PREFIX = ("utm_",)
TRACKING_KEYS = {"gclid", "fbclid", "yclid", "igshid", "mc_cid", "mc_eid", "ref", "spm", "from"}


def canonical_url(u: str) -> str:
    """去除常见追踪参数、fragment，统一 scheme/host 小写。"""
    try:
        p = urlparse(u)
        scheme = p.scheme.lower()
        netloc = p.netloc.lower()
        path = p.path or "/"
        fragment = ""
        q = []
        for k, v in parse_qsl(p.query, keep_blank_values=True):
            kl = k.lower()
            if kl in TRACKING_KEYS:
                continue
            if any(kl.startswith(pref) for pref in TRACKING_KEYS_PREFIX):
                continue
            q.append((k, v))
        query = urlencode(q, doseq=True)
        return urlunparse((scheme, netloc, path, p.params, query, fragment))
    except Exception:
        return u


def host_of(u: str) -> str:
    try:
        return urlparse(u).netloc.lower()
    except Exception:
        return ""


def dedupe_and_cluster(results: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    if not results:
        return results
    seen = set()
    per_host: Dict[str, int] = {}
    out = []
    for it in results:
        url = it.get("url", "") or ""
        if not is_http_url(url):
            continue
        cu = canonical_url(url)
        h = host_of(cu)
        if DEDUP and cu in seen:
            continue
        if MAX_PER_HOST > 0:
            per_host.setdefault(h, 0)
            if per_host[h] >= MAX_PER_HOST:
                continue
            per_host[h] += 1
        seen.add(cu)
        it["url"] = cu
        out.append(it)
    return out


# ---------- SSRF Protection ----------
def _domain_blocked(host: str) -> bool:
    host = (host or "").lower().strip(".")
    if not host:
        return True

    for b in BLOCK_DOMAINS:
        b = b.strip(".")
        if host == b or host.endswith("." + b):
            return True

    if ALLOW_DOMAINS:
        ok = False
        for a in ALLOW_DOMAINS:
            a = a.strip(".")
            if host == a or host.endswith("." + a):
                ok = True
                break
        if not ok:
            return True

    return False


def _ip_blocked(ip: ipaddress._BaseAddress) -> bool:
    for net in BLOCK_IP_NETWORKS:
        if ip in net:
            return True
    return False


def ssrf_guard(url: str) -> None:
    """
    拒绝：
      - 非 http(s)
      - 目标域名不符合 allow/block
      - 解析到私网/本机/metadata 等 IP
    """
    if not is_http_url(url):
        raise HTTPException(400, "Invalid url scheme")

    p = urlparse(url)
    host = (p.hostname or "").lower()
    if _domain_blocked(host):
        raise HTTPException(403, f"Blocked by domain policy: {host}")

    # 如果 host 本身是 IP，直接检查
    try:
        ip = ipaddress.ip_address(host)
        if _ip_blocked(ip):
            raise HTTPException(403, f"Blocked IP: {ip}")
        return
    except ValueError:
        pass

    # DNS 解析校验
    try:
        infos = socket.getaddrinfo(host, None)
        for family, _, _, _, sockaddr in infos:
            ip_str = sockaddr[0]
            ip_obj = ipaddress.ip_address(ip_str)
            if _ip_blocked(ip_obj):
                raise HTTPException(403, f"Blocked resolved IP: {ip_obj}")
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(403, f"DNS resolution failed for host: {host}")


# ===================== Models =====================
class SearchRequest(BaseModel):
    query: str = Field(..., min_length=1)
    count: int = Field(5, ge=1, le=10)
    provider: Optional[str] = None
    enrich: bool = False
    max_chars: int = Field(8000, ge=1000, le=50000)
    render: Optional[bool] = None
    concurrency: int = Field(3, ge=1, le=8)


class FetchRequest(BaseModel):
    url: str
    render: Optional[bool] = None
    max_chars: int = Field(20000, ge=1000, le=100000)
    timeout: Optional[float] = None
    headers: Optional[Dict[str, str]] = None
    bypass_cache: bool = False


class CookieUpdateRequest(BaseModel):
    cookies: Optional[List[Dict[str, Any]]] = None
    raw: Optional[str] = None


class MCPCallRequest(BaseModel):
    tool: str
    arguments: Dict[str, Any] = Field(default_factory=dict)


# ===================== Providers =====================
async def tavily_search(q: str, count: int) -> List[Dict[str, Any]]:
    if not TAVILY_API_KEY:
        raise RuntimeError("Tavily key missing")
    headers = {"Authorization": f"Bearer {TAVILY_API_KEY}", "Content-Type": "application/json"}
    payload = {"query": q, "max_results": count, "search_depth": "basic"}
    res = await http_client.post("https://api.tavily.com/search", json=payload, headers=headers)
    res.raise_for_status()
    data = res.json()
    out = []
    for it in (data.get("results") or [])[:count]:
        out.append(normalize(it.get("title", ""), it.get("url", ""), it.get("content", ""), "tavily"))
    return out


async def serper_search(q: str, count: int) -> List[Dict[str, Any]]:
    if not SERPER_API_KEY:
        raise RuntimeError("Serper key missing")
    headers = {"X-API-KEY": SERPER_API_KEY, "Content-Type": "application/json"}
    payload = {"q": q, "num": count}
    res = await http_client.post("https://google.serper.dev/search", json=payload, headers=headers)
    res.raise_for_status()
    data = res.json()
    out = []
    for it in (data.get("organic") or [])[:count]:
        out.append(normalize(it.get("title", ""), it.get("link", ""), it.get("snippet", ""), "serper"))
    return out


async def searxng_search(q: str, count: int) -> List[Dict[str, Any]]:
    params = {"q": q, "format": "json"}
    res = await http_client.get(f"{SEARXNG_URL}/search", params=params)
    res.raise_for_status()
    data = res.json()
    out = []
    for it in (data.get("results") or [])[:count]:
        snippet = it.get("content") or it.get("snippet") or ""
        out.append(normalize(it.get("title", ""), it.get("url", ""), snippet, "searxng"))
    return out


PROVIDERS = {"tavily": tavily_search, "serper": serper_search, "searxng": searxng_search}


async def do_search(q: str, count: int, force_provider: Optional[str]) -> Dict[str, Any]:
    if force_provider:
        fn = PROVIDERS.get(force_provider)
        if not fn:
            raise HTTPException(400, f"Unknown provider: {force_provider}")
        results = await fn(q, count)
        return {"provider": force_provider, "results": results}

    last_err = None
    for name in ORDER:
        fn = PROVIDERS.get(name)
        if not fn:
            continue
        try:
            results = await fn(q, count)
            if results:
                log.info("search provider=%s query=%r count=%d", name, q, len(results))
                return {"provider": name, "results": results}
        except Exception as e:
            last_err = f"{name}: {type(e).__name__} {e}"
            log.warning("search provider=%s failed: %s", name, last_err)
    raise HTTPException(502, f"All providers failed. Last error: {last_err}")


# ===================== Fetch (anti-bot) =====================
async def fetch_via_browserless(url: str, timeout: float, cookies: Optional[List[Dict[str, Any]]] = None) -> Tuple[int, str, Dict[str, str]]:
    if not BROWSERLESS_TOKEN:
        raise RuntimeError("BROWSERLESS_TOKEN missing")

    endpoint = f"{BROWSERLESS_HTTP}/content"
    params = {"token": BROWSERLESS_TOKEN}
    payload: Dict[str, Any] = {
        "url": url,
        "gotoOptions": {"waitUntil": "networkidle2", "timeout": int(timeout * 1000)},
    }
    if cookies:
        payload["cookies"] = cookies
        log.info("injecting %d cookies for %s", len(cookies), host_of(url))

    res = await http_client.post(endpoint, params=params, json=payload, timeout=timeout + 10)
    res.raise_for_status()
    html = res.text
    headers = {k.lower(): v for k, v in res.headers.items()}
    return res.status_code, html, headers


async def fetch_via_httpx(url: str, timeout: float, extra_headers: Optional[Dict[str, str]] = None) -> Tuple[int, str, Dict[str, str]]:
    headers = {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.8,zh-CN;q=0.7,zh;q=0.6,ja;q=0.5",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    }
    if extra_headers:
        headers.update(extra_headers)

    res = await http_client.get(url, headers=headers, timeout=timeout)
    res.raise_for_status()

    content = res.text
    if len(content) > MAX_FETCH_BYTES:
        content = content[:MAX_FETCH_BYTES]

    headers_out = {k.lower(): v for k, v in res.headers.items()}
    return res.status_code, content, headers_out


def _extract_note_cards(soup: BeautifulSoup) -> Optional[str]:
    """提取小红书等平台的笔记卡片列表，返回格式化文本或 None。"""
    cards = soup.select("section.note-item, div.note-item, a.note-item")
    if len(cards) < 2:
        return None
    lines = []
    for i, card in enumerate(cards, 1):
        title_el = card.select_one("[class*=title]")
        author_el = card.select_one("[class*=author], [class*=name]")
        likes_el = card.select_one("[class*=like], [class*=count]")
        link_el = card.find("a", href=True)
        t = title_el.get_text(strip=True) if title_el else ""
        a = author_el.get_text(strip=True) if author_el else ""
        lk = likes_el.get_text(strip=True) if likes_el else ""
        href = link_el["href"] if link_el else ""
        parts = [f"{i}. {t}"]
        if a:
            parts.append(f"   作者: {a}")
        if lk:
            parts.append(f"   点赞: {lk}")
        if href:
            parts.append(f"   链接: {href}")
        lines.append("\n".join(parts))
    return "\n\n".join(lines)


def extract_from_html(html: str, url: str, max_chars: int) -> Dict[str, Any]:
    html = html or ""

    # 先解析一次提取 title
    soup = BeautifulSoup(html, "lxml")
    title = ""
    if soup.title and soup.title.string:
        title = soup.title.string.strip()

    # 优先：笔记卡片提取（小红书搜索结果等）
    text = _extract_note_cards(soup) or ""

    # trafilatura 提取正文
    if not text:
        try:
            downloaded = trafilatura.extract(
                html,
                url=url,
                include_comments=False,
                include_tables=False,
                include_formatting=False,
                favor_precision=True,
            )
            if downloaded:
                text = downloaded
        except Exception:
            pass

    # fallback: BeautifulSoup 纯文本（复用已解析的 soup）
    if not text:
        for tag in soup(["script", "style", "noscript"]):
            tag.decompose()
        text = soup.get_text(separator="\n")

    text = strip_junk_whitespace(text)
    text = truncate_text(text, max_chars)

    return {"title": title, "text": text}


async def fetch_and_extract(url: str, render: bool, timeout: float, max_chars: int, extra_headers: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    url = canonical_url(url)
    ssrf_guard(url)

    if render:
        cookies = get_cookies_for_url(url)
        status_code, html, resp_headers = await fetch_via_browserless(url, timeout, cookies=cookies or None)
    else:
        status_code, html, resp_headers = await fetch_via_httpx(url, timeout, extra_headers)

    extracted = extract_from_html(html, url, max_chars=max_chars)
    text = extracted.get("text") or ""
    result = {
        "url": url,
        "status_code": status_code,
        "render": render,
        "title": extracted.get("title") or "",
        "text": text,
        "content_type": resp_headers.get("content-type", ""),
    }

    if detect_needs_login(text, url, html=html, status_code=status_code, title=extracted.get("title") or ""):
        result["needs_login"] = True
        result["has_cookies"] = bool(get_cookies_for_url(url))

    return result


# ===================== Endpoints =====================
@app.get("/health")
async def health(req: Request):
    await enforce_auth_and_ratelimit(req)
    try:
        await rds.ping()
        redis_ok = True
    except Exception:
        redis_ok = False
    return {
        "ok": redis_ok,
        "redis": redis_ok,
        "order": ORDER,
        "browserless_configured": bool(BROWSERLESS_TOKEN),
        "dedupe": {"enabled": DEDUP, "max_per_host": MAX_PER_HOST},
    }


@app.post("/search")
async def search(req: Request, payload: SearchRequest):
    await enforce_auth_and_ratelimit(req)

    q = payload.query.strip()
    count = payload.count
    enrich = payload.enrich
    max_chars = payload.max_chars
    render = payload.render if payload.render is not None else FETCH_DEFAULT_RENDER
    concurrency = payload.concurrency

    # 缓存 key 不含 key_id，所有调用者共享搜索缓存
    cache_key = "cache:search:" + sha(
        f"{q}|{count}|{payload.provider}|{','.join(ORDER)}|enrich={enrich}|render={render}|mc={max_chars}|dedup={DEDUP}|mph={MAX_PER_HOST}"
    )
    cached = await rds.get(cache_key)
    if cached:
        return {"query": q, "count": count, "cached": True, **json.loads(cached)}

    result = await do_search(q, count, payload.provider)
    results = result["results"]

    # 去重 + 同站聚类
    results = dedupe_and_cluster(results)

    if enrich:
        import asyncio
        sem = asyncio.Semaphore(concurrency)
        enrich_timeout = min(ENRICH_PER_PAGE_TIMEOUT, FETCH_TIMEOUT)

        async def enrich_one(it: Dict[str, Any]) -> Dict[str, Any]:
            url = it.get("url") or ""
            if not is_http_url(url):
                return {**it, "fetched": False, "content": "", "fetch_error": "invalid_url"}
            try:
                async with sem:
                    f_cache_key = "cache:fetch:" + sha(f"{url}|render={render}|mc={max_chars}")
                    c2 = await rds.get(f_cache_key)
                    if c2:
                        data2 = json.loads(c2)
                        return {**it, "fetched": True, "content": data2.get("text", ""), "page_title": data2.get("title", ""), "render": render, "cached_fetch": True}

                    data = await fetch_and_extract(url, render=render, timeout=enrich_timeout, max_chars=max_chars)
                    await rds.setex(f_cache_key, CACHE_TTL, json.dumps(data, ensure_ascii=False))
                    return {**it, "fetched": True, "content": data.get("text", ""), "page_title": data.get("title", ""), "render": render, "cached_fetch": False}
            except Exception as e:
                log.warning("enrich failed url=%s: %s", url, e)
                return {**it, "fetched": False, "content": "", "fetch_error": f"{type(e).__name__}: {e}"}

        # Wall-clock timeout: return partial results instead of hanging
        tasks_map: Dict[asyncio.Task, int] = {}
        for i, it in enumerate(results):
            task = asyncio.create_task(enrich_one(it))
            tasks_map[task] = i

        done, pending = await asyncio.wait(tasks_map.keys(), timeout=ENRICH_WALL_TIMEOUT)

        # Cancel any still-running tasks
        for t in pending:
            t.cancel()

        # Build enriched results, preserving original order
        enriched: List[Dict[str, Any]] = [{}] * len(results)
        for task, idx in tasks_map.items():
            if task in done:
                try:
                    enriched[idx] = task.result()
                except Exception as e:
                    enriched[idx] = {**results[idx], "fetched": False, "content": "", "fetch_error": f"{type(e).__name__}: {e}"}
            else:
                enriched[idx] = {**results[idx], "fetched": False, "content": "", "fetch_error": "timeout (wall clock exceeded)"}

        if pending:
            log.warning("enrich wall-clock timeout: %d/%d pages timed out", len(pending), len(results))

        results = enriched

    out = {"provider": result["provider"], "results": results}
    await rds.setex(cache_key, CACHE_TTL, json.dumps(out, ensure_ascii=False))
    return {"query": q, "count": count, "cached": False, **out}


@app.post("/fetch")
async def fetch(req: Request, payload: FetchRequest):
    await enforce_auth_and_ratelimit(req)

    url = payload.url.strip()
    render = payload.render if payload.render is not None else FETCH_DEFAULT_RENDER
    timeout = float(payload.timeout) if payload.timeout else FETCH_TIMEOUT
    max_chars = payload.max_chars
    extra_headers = payload.headers or None

    cache_key = "cache:fetch:" + sha(f"{canonical_url(url)}|render={render}|mc={max_chars}|to={timeout}")

    if not payload.bypass_cache:
        cached = await rds.get(cache_key)
        if cached:
            return {"cached": True, **json.loads(cached)}

    data = await fetch_and_extract(url, render=render, timeout=timeout, max_chars=max_chars, extra_headers=extra_headers)
    await rds.setex(cache_key, CACHE_TTL, json.dumps(data, ensure_ascii=False))
    return {"cached": False, **data}


# ===================== Firecrawl-compatible API =====================
class FirecrawlScrapeRequest(BaseModel):
    url: str
    formats: Optional[List[str]] = None
    onlyMainContent: Optional[bool] = True
    timeout: Optional[int] = None
    waitFor: Optional[int] = None


def html_to_markdown(html: str, url: str, max_chars: int) -> str:
    """Extract text content (Firecrawl returns markdown, we return clean text)."""
    result = extract_from_html(html, url, max_chars)
    return result.get("text", "")


@app.post("/v1/scrape")
async def firecrawl_scrape(req: Request, payload: FirecrawlScrapeRequest):
    # Firecrawl uses Bearer token auth
    auth_header = (req.headers.get("Authorization") or "").strip()
    token = ""
    if auth_header.startswith("Bearer "):
        token = auth_header[7:].strip()
    if API_KEYS and token not in API_KEYS:
        raise HTTPException(401, "Unauthorized")

    url = payload.url.strip()
    timeout = (payload.timeout or int(FETCH_TIMEOUT * 1000)) / 1000.0
    max_chars = 50000

    try:
        data = await fetch_and_extract(url, render=True, timeout=timeout, max_chars=max_chars)
        formats = payload.formats or ["markdown"]
        result: Dict[str, Any] = {
            "metadata": {
                "title": data.get("title", ""),
                "sourceURL": data.get("url", url),
                "statusCode": data.get("status_code", 200),
            }
        }
        text = data.get("text", "")
        if "markdown" in formats:
            result["markdown"] = text
        if "text" in formats or "rawHtml" in formats:
            result["text"] = text

        return {"success": True, "data": result}
    except HTTPException as e:
        return {"success": False, "error": e.detail}
    except Exception as e:
        log.warning("firecrawl scrape failed url=%s: %s", url, e)
        return {"success": False, "error": f"{type(e).__name__}: {e}"}


# ===================== Cookie Management =====================
@app.post("/cookies/reload")
async def cookies_reload(req: Request):
    await enforce_auth_and_ratelimit(req)
    count = load_cookies()
    return {"ok": True, "domains": count, "loaded": list(_domain_cookies.keys())}


@app.get("/cookies")
async def list_cookies(req: Request):
    await enforce_auth_and_ratelimit(req)
    domains = {}
    for domain, cookies in _domain_cookies.items():
        domains[domain] = {
            "count": len(cookies),
            "names": [c.get("name", "") for c in cookies],
        }
    return {"ok": True, "domains": domains}


@app.put("/cookies/{domain}")
async def update_cookies(domain: str, req: Request, payload: CookieUpdateRequest):
    await enforce_auth_and_ratelimit(req)
    domain = domain.lower().strip(".")

    if payload.raw:
        cookies = parse_raw_cookie_string(payload.raw, domain)
    elif payload.cookies:
        cookies = []
        for c in payload.cookies:
            if "name" not in c or "value" not in c:
                continue
            c.setdefault("domain", f".{domain}")
            c.setdefault("path", "/")
            cookies.append(c)
    else:
        raise HTTPException(400, "Provide 'raw' (cookie string) or 'cookies' (JSON array)")

    if not cookies:
        raise HTTPException(400, "No valid cookies parsed")

    _domain_cookies[domain] = cookies
    save_cookies()
    return {
        "ok": True,
        "domain": domain,
        "count": len(cookies),
        "names": [c["name"] for c in cookies],
    }


@app.delete("/cookies/{domain}")
async def delete_cookies(domain: str, req: Request):
    await enforce_auth_and_ratelimit(req)
    domain = domain.lower().strip(".")
    if domain not in _domain_cookies:
        raise HTTPException(404, f"No cookies for domain: {domain}")
    del _domain_cookies[domain]
    save_cookies()
    return {"ok": True, "domain": domain, "deleted": True}


# ===================== MCP (HTTP flavor) =====================
@app.get("/mcp/tools")
async def mcp_tools(req: Request):
    await enforce_auth_and_ratelimit(req)
    return {
        "tools": [
            {
                "name": "search",
                "description": "Search web using Tavily/Serper/SearXNG with fallback. Optionally enrich results by fetching page text.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "count": {"type": "integer", "minimum": 1, "maximum": 10, "default": 5},
                        "provider": {"type": "string", "enum": ["tavily", "serper", "searxng"]},
                        "enrich": {"type": "boolean", "default": False},
                        "max_chars": {"type": "integer", "default": 8000},
                        "render": {"type": "boolean"},
                        "concurrency": {"type": "integer", "default": 3},
                    },
                    "required": ["query"],
                },
            },
            {
                "name": "fetch",
                "description": "Fetch a URL and extract main text. Uses Browserless render by default for anti-bot robustness.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "url": {"type": "string"},
                        "render": {"type": "boolean"},
                        "max_chars": {"type": "integer", "default": 20000},
                        "timeout": {"type": "number"},
                        "headers": {"type": "object"},
                    },
                    "required": ["url"],
                },
            },
        ]
    }


@app.post("/mcp/call")
async def mcp_call(req: Request, payload: MCPCallRequest):
    await enforce_auth_and_ratelimit(req)
    tool = payload.tool
    args = payload.arguments or {}

    if tool == "search":
        sr = SearchRequest(**args)
        return await search(req, sr)

    if tool == "fetch":
        fr = FetchRequest(**args)
        return await fetch(req, fr)

    raise HTTPException(400, f"Unknown tool: {tool}")


# ===================== Cookie Catcher (remote browser login) =====================
import cookie_catcher


@app.get("/cookie-catcher")
async def cookie_catcher_page(req: Request):
    """Serve the Cookie Catcher HTML page."""
    if API_KEYS:
        k = get_api_key(req)
        if not k or k not in API_KEYS:
            raise HTTPException(401, "Unauthorized: pass ?key=YOUR_API_KEY")
    return FileResponse("static/cookie-catcher.html", media_type="text/html")


@app.websocket("/cookie-catcher/ws")
async def cookie_catcher_ws(ws: WebSocket):
    """WebSocket bridge between user browser and remote Chrome CDP session."""
    key = ws.query_params.get("key", "")
    if API_KEYS and key not in API_KEYS:
        await ws.close(code=4001, reason="Unauthorized")
        return

    await ws.accept()

    if not cookie_catcher.can_create():
        await ws.send_json({
            "type": "error",
            "message": f"Too many active sessions (max {cookie_catcher.MAX_SESSIONS})",
        })
        await ws.close()
        return

    session = cookie_catcher.CatcherSession(BROWSERLESS_HTTP, BROWSERLESS_TOKEN)

    # Wire callbacks — forward CDP events to the user's WebSocket
    async def on_frame(data: str, meta: dict):
        try:
            await ws.send_json({"type": "frame", "data": data})
        except Exception:
            pass

    async def on_url(url: str):
        try:
            await ws.send_json({"type": "url", "url": url})
        except Exception:
            pass

    async def on_title(title: str):
        try:
            await ws.send_json({"type": "title", "title": title})
        except Exception:
            pass

    async def on_close():
        try:
            await ws.send_json({"type": "closed"})
        except Exception:
            pass

    session.on_frame = on_frame
    session.on_url = on_url
    session.on_title = on_title
    session.on_close = on_close

    try:
        while True:
            msg = await ws.receive_json()
            t = msg.get("type")

            if t == "navigate":
                url = (msg.get("url") or "").strip()
                if not url:
                    await ws.send_json({"type": "error", "message": "URL required"})
                    continue
                try:
                    if not session._ws:
                        await session.start(url)
                    else:
                        await session.navigate(url)
                except Exception as e:
                    await ws.send_json({"type": "error", "message": str(e)})

            elif t == "mouse":
                try:
                    await session.mouse(
                        msg.get("action", ""),
                        msg.get("x", 0), msg.get("y", 0),
                        msg.get("button", "left"),
                        msg.get("clickCount", 1),
                    )
                except Exception:
                    pass

            elif t == "key":
                try:
                    await session.keyboard(
                        msg.get("action", ""),
                        msg.get("key", ""),
                        msg.get("code", ""),
                        msg.get("text", ""),
                        msg.get("modifiers", 0),
                        msg.get("keyCode", 0),
                    )
                except Exception:
                    pass

            elif t == "scroll":
                try:
                    await session.scroll(
                        msg.get("x", 0), msg.get("y", 0),
                        msg.get("deltaX", 0), msg.get("deltaY", 0),
                    )
                except Exception:
                    pass

            elif t == "save_cookies":
                try:
                    result = await session.extract_cookies(msg.get("domain", ""))
                    cookies = result["cookies"]
                    domain = result["domain"]
                    if not cookies:
                        await ws.send_json({
                            "type": "error",
                            "message": f"No cookies found for {domain} ({result['total']} total in browser)",
                        })
                        continue
                    # Save via existing cookie persistence
                    _domain_cookies[domain] = cookies
                    save_cookies()
                    await ws.send_json({
                        "type": "cookies_saved",
                        "domain": domain,
                        "count": len(cookies),
                        "names": [c["name"] for c in cookies],
                    })
                    # Auto-close after saving — give frontend time to show toast
                    await asyncio.sleep(2)
                    await session.close()
                    break
                except Exception as e:
                    await ws.send_json({"type": "error", "message": f"Save failed: {e}"})

            elif t == "close":
                await session.close()
                break

    except WebSocketDisconnect:
        pass
    except Exception as e:
        log.warning("cookie-catcher ws error: %s", e)
    finally:
        await session.close()
