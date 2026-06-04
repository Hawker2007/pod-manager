"""
fetch_tool.py — Agent-friendly HTTP fetch tool for LLM tool collections.

Provides clean markdown content extraction, link resolution, structured
ToolResult envelopes, and async multi-fetch. Designed for intranet/internal
service URLs where raw HTML is too noisy for LLM context windows.

Dependencies:
    pip install httpx readability-lxml markdownify beautifulsoup4

Usage:
    from fetch_tool import fetch_url, fetch_multiple, FetchResult

    result = fetch_url("http://intranet/wiki/deployment")
    if result.ok:
        print(result.content)
        print(result.links)
"""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass, field
from typing import Literal
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup
from markdownify import markdownify as to_markdown
from readability import Document

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Rough chars-per-token estimate (conservative for mixed content)
_CHARS_PER_TOKEN = 3.5

# Content types we attempt to extract text from
_TEXT_TYPES = {
    "text/html",
    "text/plain",
    "application/json",
    "application/xml",
    "text/xml",
    "application/xhtml+xml",
}

# Tags that are pure noise — stripped before markdown conversion
_NOISE_TAGS = [
    "script", "style", "noscript", "iframe",
    "nav", "header", "footer", "aside",
    "form", "button", "input", "select",
    "svg", "canvas", "figure",
    "[document]", "head",
]

# Default headers that look like a normal browser enough for intranet services
_DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------

@dataclass
class FetchResult:
    """Structured result returned to the agent for every fetch operation."""

    # Request info
    url: str                          # final URL after redirects
    original_url: str                 # URL as requested

    # Response metadata
    ok: bool                          # True if 2xx and content extracted
    status: int                       # HTTP status code
    content_type: str                 # normalized content-type (no params)
    elapsed_ms: float                 # wall time for the request

    # Extracted content
    title: str = ""                   # page <title> or first h1
    content: str = ""                 # cleaned, possibly truncated text/markdown
    links: list[dict] = field(default_factory=list)  # [{text, href}, ...]

    # Budget / size info
    truncated: bool = False
    byte_size: int = 0                # raw response body size
    token_estimate: int = 0           # estimated tokens in content

    # Error info (populated when ok=False)
    error: str = ""

    def to_dict(self) -> dict:
        return {
            "url": self.url,
            "original_url": self.original_url,
            "ok": self.ok,
            "status": self.status,
            "content_type": self.content_type,
            "elapsed_ms": round(self.elapsed_ms, 1),
            "title": self.title,
            "content": self.content,
            "links": self.links,
            "truncated": self.truncated,
            "byte_size": self.byte_size,
            "token_estimate": self.token_estimate,
            "error": self.error,
        }

    def __repr__(self) -> str:
        status_str = f"HTTP {self.status}"
        trunc_str = " [truncated]" if self.truncated else ""
        err_str = f" ERROR: {self.error}" if self.error else ""
        return (
            f"FetchResult({status_str} {self.url!r} "
            f"~{self.token_estimate}tok{trunc_str}{err_str})"
        )


# ---------------------------------------------------------------------------
# Auth hook (extend for Kerberos, SSO, etc.)
# ---------------------------------------------------------------------------

class AuthResolver:
    """
    Pluggable auth resolver. Register URL prefix patterns → header factories.

    Example:
        resolver = AuthResolver()
        resolver.register("http://internal-api/", lambda: {"X-Api-Key": "secret"})

        # For Basic auth:
        import base64
        def basic(user, pw):
            token = base64.b64encode(f"{user}:{pw}".encode()).decode()
            return lambda: {"Authorization": f"Basic {token}"}
        resolver.register("http://wiki/", basic("admin", "pass"))
    """

    def __init__(self):
        self._rules: list[tuple[str, callable]] = []

    def register(self, url_prefix: str, header_factory: callable) -> None:
        """Register a header factory for all URLs starting with url_prefix."""
        self._rules.append((url_prefix, header_factory))

    def resolve(self, url: str) -> dict:
        """Return auth headers for the given URL, or empty dict."""
        for prefix, factory in self._rules:
            if url.startswith(prefix):
                return factory()
        return {}


# Module-level resolver — configure once at startup
auth_resolver = AuthResolver()


# ---------------------------------------------------------------------------
# Internal extraction helpers
# ---------------------------------------------------------------------------

def _normalize_content_type(raw: str) -> str:
    """Strip params from Content-Type: 'text/html; charset=utf-8' → 'text/html'"""
    return raw.split(";")[0].strip().lower() if raw else "application/octet-stream"


def _estimate_tokens(text: str) -> int:
    return max(1, int(len(text) / _CHARS_PER_TOKEN))


def _truncate_to_budget(text: str, max_tokens: int) -> tuple[str, bool]:
    """Truncate text to fit within token budget. Returns (text, was_truncated)."""
    if max_tokens <= 0:
        return text, False
    max_chars = int(max_tokens * _CHARS_PER_TOKEN)
    if len(text) <= max_chars:
        return text, False
    # Truncate at a word boundary if possible
    truncated = text[:max_chars]
    last_newline = truncated.rfind("\n")
    if last_newline > max_chars * 0.8:
        truncated = truncated[:last_newline]
    return truncated + "\n\n[... content truncated ...]", True


def _extract_links(soup: BeautifulSoup, base_url: str) -> list[dict]:
    """Extract all <a href> links, resolve to absolute URLs, deduplicate."""
    seen: set[str] = set()
    links = []
    for tag in soup.find_all("a", href=True):
        href = tag["href"].strip()
        # Skip anchors, javascript, mailto
        if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
            continue
        abs_href = urljoin(base_url, href)
        # Only include http/https
        if not abs_href.startswith(("http://", "https://")):
            continue
        if abs_href in seen:
            continue
        seen.add(abs_href)
        text = tag.get_text(strip=True) or abs_href
        links.append({"text": text[:120], "href": abs_href})
    return links


def _extract_title(soup: BeautifulSoup, doc: Document | None) -> str:
    """Best-effort title extraction: readability title → <title> → first h1."""
    if doc:
        t = doc.title()
        if t and t.strip():
            return t.strip()
    title_tag = soup.find("title")
    if title_tag:
        t = title_tag.get_text(strip=True)
        if t:
            return t
    h1 = soup.find("h1")
    if h1:
        return h1.get_text(strip=True)
    return ""


def _html_to_markdown(
    html: str,
    base_url: str,
    use_readability: bool = True,
) -> tuple[str, str, list[dict]]:
    """
    Convert HTML to clean markdown.

    Returns: (title, markdown_content, links)
    """
    soup = BeautifulSoup(html, "html.parser")
    links = _extract_links(soup, base_url)

    doc = None
    if use_readability:
        try:
            doc = Document(html)
            content_html = doc.summary(html_partial=True)
        except Exception:
            content_html = html
    else:
        content_html = html

    # Clean noise tags from content
    content_soup = BeautifulSoup(content_html, "html.parser")
    for tag_name in _NOISE_TAGS:
        for tag in content_soup.find_all(tag_name):
            tag.decompose()

    title = _extract_title(soup, doc)

    # Convert to markdown
    md = to_markdown(
        str(content_soup),
        heading_style="ATX",
        bullets="-",
        strip=["a", "img"],   # strip link/img markup, keep text
    )

    # Post-process: collapse excessive blank lines
    md = re.sub(r"\n{3,}", "\n\n", md).strip()

    return title, md, links


def _format_json(raw: str, max_tokens: int) -> str:
    """Pretty-print JSON, truncating if needed."""
    import json
    try:
        parsed = json.loads(raw)
        pretty = json.dumps(parsed, indent=2, ensure_ascii=False)
        # If it's very large, summarize structure instead
        if _estimate_tokens(pretty) > max_tokens:
            if isinstance(parsed, list):
                return f"[JSON Array — {len(parsed)} items]\nFirst item:\n{json.dumps(parsed[0], indent=2)}"
            elif isinstance(parsed, dict):
                keys = list(parsed.keys())
                return f"[JSON Object — {len(keys)} keys: {keys[:20]}]\n{json.dumps({k: parsed[k] for k in keys[:5]}, indent=2)}"
        return pretty
    except Exception:
        return raw


# ---------------------------------------------------------------------------
# Core async fetch
# ---------------------------------------------------------------------------

async def _async_fetch_one(
    client: httpx.AsyncClient,
    url: str,
    extract_mode: Literal["auto", "full", "links_only", "raw"],
    max_tokens: int,
    extra_headers: dict,
) -> FetchResult:
    """Internal: fetch a single URL using a shared httpx client."""
    start = time.monotonic()
    original_url = url

    def _error(msg: str, status: int = 0) -> FetchResult:
        return FetchResult(
            url=url,
            original_url=original_url,
            ok=False,
            status=status,
            content_type="",
            elapsed_ms=(time.monotonic() - start) * 1000,
            error=msg,
        )

    try:
        auth_headers = auth_resolver.resolve(url)
        headers = {**extra_headers, **auth_headers}
        response = await client.get(url, headers=headers)
    except httpx.TimeoutException:
        return _error("Request timed out")
    except httpx.ConnectError as e:
        return _error(f"Connection failed: {e}")
    except httpx.RequestError as e:
        return _error(f"Request error: {e}")

    elapsed = (time.monotonic() - start) * 1000
    final_url = str(response.url)
    content_type = _normalize_content_type(response.headers.get("content-type", ""))
    byte_size = len(response.content)
    ok = response.is_success

    if not ok:
        return FetchResult(
            url=final_url,
            original_url=original_url,
            ok=False,
            status=response.status_code,
            content_type=content_type,
            elapsed_ms=elapsed,
            byte_size=byte_size,
            error=f"HTTP {response.status_code}",
        )

    # raw mode — return body as-is (still truncated)
    if extract_mode == "raw":
        try:
            raw_text = response.text
        except Exception:
            raw_text = response.content.decode("utf-8", errors="replace")
        content, truncated = _truncate_to_budget(raw_text, max_tokens)
        return FetchResult(
            url=final_url,
            original_url=original_url,
            ok=True,
            status=response.status_code,
            content_type=content_type,
            elapsed_ms=elapsed,
            content=content,
            truncated=truncated,
            byte_size=byte_size,
            token_estimate=_estimate_tokens(content),
        )

    # Non-text content types — signal type but don't dump bytes
    if content_type not in _TEXT_TYPES:
        return FetchResult(
            url=final_url,
            original_url=original_url,
            ok=True,
            status=response.status_code,
            content_type=content_type,
            elapsed_ms=elapsed,
            content=f"[Binary content: {content_type}, {byte_size} bytes]",
            byte_size=byte_size,
            token_estimate=10,
        )

    try:
        body = response.text
    except Exception:
        body = response.content.decode("utf-8", errors="replace")

    # JSON
    if "json" in content_type:
        content = _format_json(body, max_tokens)
        content, truncated = _truncate_to_budget(content, max_tokens)
        return FetchResult(
            url=final_url,
            original_url=original_url,
            ok=True,
            status=response.status_code,
            content_type=content_type,
            elapsed_ms=elapsed,
            title=urlparse(final_url).path,
            content=content,
            truncated=truncated,
            byte_size=byte_size,
            token_estimate=_estimate_tokens(content),
        )

    # Plain text / XML
    if content_type in ("text/plain", "application/xml", "text/xml"):
        content, truncated = _truncate_to_budget(body, max_tokens)
        return FetchResult(
            url=final_url,
            original_url=original_url,
            ok=True,
            status=response.status_code,
            content_type=content_type,
            elapsed_ms=elapsed,
            title=urlparse(final_url).path,
            content=content,
            truncated=truncated,
            byte_size=byte_size,
            token_estimate=_estimate_tokens(content),
        )

    # HTML — main path
    use_readability = extract_mode in ("auto", "full")
    title, md, links = _html_to_markdown(body, final_url, use_readability=use_readability)

    if extract_mode == "links_only":
        content = ""
        truncated = False
    else:
        content, truncated = _truncate_to_budget(md, max_tokens)

    return FetchResult(
        url=final_url,
        original_url=original_url,
        ok=True,
        status=response.status_code,
        content_type=content_type,
        elapsed_ms=elapsed,
        title=title,
        content=content,
        links=links,
        truncated=truncated,
        byte_size=byte_size,
        token_estimate=_estimate_tokens(content),
    )


# ---------------------------------------------------------------------------
# Event loop helper
# ---------------------------------------------------------------------------

def _run_async(coro):
    """
    Run an async coroutine safely, handling both fresh and existing event loops.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # No running event loop — safe to use asyncio.run()
        return asyncio.run(coro)
    
    # There's already a running event loop.
    # Create a new loop in a background thread or use nest_asyncio if available.
    import threading
    result = None
    exception = None
    
    def _run_in_new_loop():
        nonlocal result, exception
        try:
            new_loop = asyncio.new_event_loop()
            asyncio.set_event_loop(new_loop)
            result = new_loop.run_until_complete(coro)
        except Exception as e:
            exception = e
        finally:
            new_loop.close()
    
    thread = threading.Thread(target=_run_in_new_loop)
    thread.start()
    thread.join()
    
    if exception:
        raise exception
    return result


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def fetch_url(
    url: str,
    *,
    extract_mode: Literal["auto", "full", "links_only", "raw"] = "auto",
    max_tokens: int = 2000,
    follow_redirects: bool = True,
    timeout: float = 10.0,
    extra_headers: dict | None = None,
) -> FetchResult:
    """
    Fetch a single URL and return a structured FetchResult.

    Args:
        url:              Target URL to fetch.
        extract_mode:     Content extraction strategy:
                            "auto"       — readability extraction → clean markdown + links (default)
                            "full"       — same as auto but skips readability heuristics (full page)
                            "links_only" — extract links only, skip content
                            "raw"        — raw response body, no extraction
        max_tokens:       Hard limit on content tokens returned. Truncates cleanly.
        follow_redirects: Whether to follow HTTP redirects.
        timeout:          Request timeout in seconds.
        extra_headers:    Additional HTTP headers to include.

    Returns:
        FetchResult with ok, status, title, content, links, token_estimate, etc.

    Example:
        result = fetch_url("http://intranet/wiki/deploy")
        if result.ok:
            print(result.content)
            for link in result.links:
                print(link["text"], "→", link["href"])
    """
    return _run_async(
        _run_fetch_one(url, extract_mode, max_tokens, follow_redirects, timeout, extra_headers or {})
    )


async def _run_fetch_one(url, extract_mode, max_tokens, follow_redirects, timeout, extra_headers):
    async with httpx.AsyncClient(
        follow_redirects=follow_redirects,
        timeout=httpx.Timeout(timeout),
        headers=_DEFAULT_HEADERS,
    ) as client:
        return await _async_fetch_one(client, url, extract_mode, max_tokens, extra_headers)


def fetch_multiple(
    urls: list[str],
    *,
    extract_mode: Literal["auto", "full", "links_only", "raw"] = "auto",
    max_tokens_per_url: int = 800,
    follow_redirects: bool = True,
    timeout: float = 10.0,
    extra_headers: dict | None = None,
    max_concurrency: int = 8,
) -> list[FetchResult]:
    """
    Fetch multiple URLs concurrently. Results preserve input order.

    Uses asyncio.gather under the hood — all requests fire simultaneously
    (up to max_concurrency). For N intranet URLs at 100ms each, total wall
    time is ~100ms instead of N*100ms.

    Args:
        urls:               List of URLs to fetch.
        extract_mode:       Applied uniformly to all URLs.
        max_tokens_per_url: Per-URL token budget (keep lower than single fetch
                            to avoid blowing total context on many URLs).
        follow_redirects:   Whether to follow HTTP redirects.
        timeout:            Per-request timeout in seconds.
        extra_headers:      Additional HTTP headers for all requests.
        max_concurrency:    Max simultaneous connections.

    Returns:
        List of FetchResult in the same order as input urls.

    Example:
        results = fetch_multiple([
            "http://intranet/service/auth/status",
            "http://intranet/service/api/status",
            "http://intranet/service/db/status",
        ])
        for r in results:
            print(r.url, "→", r.status, r.content[:200])
    """
    try:
        return _run_async(
            _run_fetch_multiple(
                urls, extract_mode, max_tokens_per_url,
                follow_redirects, timeout, extra_headers or {}, max_concurrency
            )
        )
    except Exception as e:
        # Return a list of error results instead of throwing
        # This prevents the framework from returning a dict instead of a list
        return [
            FetchResult(
                url=url,
                original_url=url,
                ok=False,
                status=0,
                content_type="",
                elapsed_ms=0,
                title="",
                content="",
                links=[],
                truncated=False,
                byte_size=0,
                token_estimate=0,
                error=f"Fetch failed: {e}",
            )
            for url in urls
        ]


async def _run_fetch_multiple(
    urls, extract_mode, max_tokens_per_url,
    follow_redirects, timeout, extra_headers, max_concurrency
):
    semaphore = asyncio.Semaphore(max_concurrency)

    async def bounded_fetch(client, url):
        async with semaphore:
            return await _async_fetch_one(client, url, extract_mode, max_tokens_per_url, extra_headers)

    async with httpx.AsyncClient(
        follow_redirects=follow_redirects,
        timeout=httpx.Timeout(timeout),
        headers=_DEFAULT_HEADERS,
    ) as client:
        tasks = [bounded_fetch(client, url) for url in urls]
        return await asyncio.gather(*tasks)

# ---------------------------------------------------------------------------
# LLM tool schema (OpenAI-compatible function calling format)
# ---------------------------------------------------------------------------

FETCH_URL_SCHEMA = {
    "name": "fetch_url",
    "description": (
        "Fetch a URL and return clean, readable content. "
        "Strips HTML noise, extracts main content as markdown, and returns navigable links. "
        "Use for intranet pages, wikis, service dashboards, APIs, or any HTTP resource. "
        "Prefer extract_mode='links_only' when you only need to discover navigation options."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "The URL to fetch.",
            },
            "extract_mode": {
                "type": "string",
                "enum": ["auto", "full", "links_only", "raw"],
                "default": "auto",
                "description": (
                    "auto: readability extraction → clean markdown + links (best for articles/wikis). "
                    "full: full page markdown + links (use when auto misses content). "
                    "links_only: skip content, return only links (fast navigation). "
                    "raw: unprocessed response body."
                ),
            },
            "max_tokens": {
                "type": "integer",
                "default": 2000,
                "description": "Max tokens to return in content. Truncates cleanly. Lower = faster reasoning.",
            },
            "follow_redirects": {
                "type": "boolean",
                "default": True,
                "description": "Whether to follow HTTP redirects.",
            },
            "timeout": {
                "type": "number",
                "default": 10.0,
                "description": "Per-request timeout in seconds.",
            },
        },
        "required": ["url"],
    },
}

FETCH_MULTIPLE_SCHEMA = {
    "name": "fetch_multiple",
    "description": (
        "Fetch multiple URLs simultaneously and return structured results for each. "
        "Use when comparing pages, checking multiple service endpoints, or following several links at once. "
        "All requests run in parallel — N URLs costs the same wall time as 1."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "urls": {
                "type": "array",
                "items": {"type": "string"},
                "description": "List of URLs to fetch.",
            },
            "extract_mode": {
                "type": "string",
                "enum": ["auto", "full", "links_only", "raw"],
                "default": "auto",
                "description": (
                    "auto: readability extraction → clean markdown + links (best for articles/wikis). "
                    "full: full page markdown + links (use when auto misses content). "
                    "links_only: skip content, return only links (fast navigation). "
                    "raw: unprocessed response body."
                ),
            },
            "max_tokens_per_url": {
                "type": "integer",
                "default": 800,
                "description": "Per-URL token budget. Keep lower than single fetch to control total context.",
            },
            "follow_redirects": {
                "type": "boolean",
                "default": True,
                "description": "Whether to follow HTTP redirects.",
            },
            "timeout": {
                "type": "number",
                "default": 10.0,
                "description": "Per-request timeout in seconds.",
            },
            "extra_headers": {
                "type": "object",
                "default": None,
                "description": "Additional HTTP headers for all requests.",
            },
            "max_concurrency": {
                "type": "integer",
                "default": 8,
                "description": "Max simultaneous connections.",
            },
        },
        "required": ["urls"],
    },
}


# ---------------------------------------------------------------------------
# Tool dispatch helper (for agent loops that call tools by name)
# ---------------------------------------------------------------------------

def dispatch(tool_name: str, tool_args: dict) -> dict | list[dict]:
    """
    Single entry point for agent tool dispatch.

    Usage in your agent loop:
        result = dispatch(tool_call.name, tool_call.arguments)
        # Returns dict (single) or list[dict] (multiple)

    Args:
        tool_name: "fetch_url" or "fetch_multiple"
        tool_args: Parsed arguments dict from the LLM tool call.

    Returns:
        Serializable dict (or list of dicts) ready to pass back as tool result.
    """
    if tool_name == "fetch_url":
        result = fetch_url(**tool_args)
        return result.to_dict()
    elif tool_name == "fetch_multiple":
        results = fetch_multiple(**tool_args)
        return [r.to_dict() for r in results]
    else:
        raise ValueError(f"Unknown tool: {tool_name!r}")


# ---------------------------------------------------------------------------
# Quick smoke test (run directly: python fetch_tool.py)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import json

    print("=" * 60)
    print("fetch_tool smoke test")
    print("=" * 60)

    # Test 1: single fetch
    print("\n[1] Single fetch — httpbin JSON")
    r = fetch_url("https://httpbin.org/json", max_tokens=500)
    print(r)
    print("content:", r.content[:300])

    # Test 2: HTML extraction
    print("\n[2] HTML fetch — example.com")
    r = fetch_url("https://example.com", max_tokens=600)
    print(r)
    print("title:", r.title)
    print("content:", r.content[:300])
    print("links:", r.links[:3])

    # Test 3: links_only
    print("\n[3] Links-only mode")
    r = fetch_url("https://example.com", extract_mode="links_only")
    print(r)
    print("links:", r.links)

    # Test 4: multi-fetch
    print("\n[4] Parallel multi-fetch")
    results = fetch_multiple(
        ["https://httpbin.org/status/200", "https://httpbin.org/status/404", "https://httpbin.org/json"],
        max_tokens_per_url=200,
    )
    for res in results:
        print(f"  {res.status} {res.url} — {res.token_estimate}tok ok={res.ok}")

    # Test 5: error handling
    print("\n[5] Error handling — unreachable host")
    r = fetch_url("http://localhost:19999/nothing", timeout=2.0)
    print(r)
    print("error:", r.error)

    # Test 6: show schema
    print("\n[6] Tool schemas (first 300 chars each)")
    print(json.dumps(FETCH_URL_SCHEMA, indent=2)[:300])
