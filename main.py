from __future__ import annotations

import os
import re
from collections import Counter
from html import unescape
from time import time
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup
from fastmcp import FastMCP
from fastmcp.server.auth import AccessToken, TokenVerifier
from pydantic import AnyHttpUrl, Field


DEFAULT_TIMEOUT_SECONDS = 12.0
DEFAULT_USER_AGENT = "Cloud-Tools-Gateway/0.1 (+https://modelcontextprotocol.io)"
MAX_RESPONSE_CHARS = 60_000


class StaticBearerAuth(TokenVerifier):
    """Minimal static bearer-token verifier for private MCP deployments."""

    async def verify_token(self, token: str) -> AccessToken | None:
        expected_token = os.getenv("MCP_BEARER_TOKEN")
        if not expected_token or token != expected_token:
            return None

        return AccessToken(
            token=token,
            client_id=os.getenv("MCP_CLIENT_ID", "authorized-client"),
            scopes=["tools:read", "tools:execute"],
            expires_at=None,
        )


mcp = FastMCP(
    name="Cloud-Tools-Gateway",
    instructions=(
        "Remote MCP tools for cloud-hosted AI agents. Authenticate with "
        "Authorization: Bearer <MCP_BEARER_TOKEN>."
    ),
    auth=StaticBearerAuth(),
)


def _clean_text(text: str) -> str:
    return re.sub(r"\s+", " ", unescape(text)).strip()


def _limit_text(text: str, max_chars: int) -> str:
    return text[:max_chars] if len(text) > max_chars else text


async def _fetch_url(
    url: str,
    *,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    max_chars: int = MAX_RESPONSE_CHARS,
) -> tuple[httpx.Response, str]:
    headers = {"User-Agent": os.getenv("HTTP_USER_AGENT", DEFAULT_USER_AGENT)}
    async with httpx.AsyncClient(
        follow_redirects=True,
        timeout=httpx.Timeout(timeout_seconds),
        headers=headers,
    ) as client:
        response = await client.get(url)
        response.raise_for_status()
        return response, _limit_text(response.text, max_chars)


@mcp.tool()
async def fetch_webpage(
    url: AnyHttpUrl,
    max_chars: int = Field(default=12_000, ge=500, le=60_000),
) -> dict[str, Any]:
    """Fetch a web page and return cleaned readable text plus basic metadata."""
    response, html = await _fetch_url(str(url), max_chars=max_chars)
    soup = BeautifulSoup(html, "html.parser")

    for tag in soup(["script", "style", "noscript", "template", "svg"]):
        tag.decompose()

    title = _clean_text(soup.title.string) if soup.title and soup.title.string else ""
    description_tag = soup.find("meta", attrs={"name": "description"})
    description = (
        _clean_text(str(description_tag.get("content", "")))
        if description_tag
        else ""
    )
    text = _clean_text(soup.get_text(" "))

    return {
        "url": str(response.url),
        "status_code": response.status_code,
        "content_type": response.headers.get("content-type", ""),
        "title": title,
        "description": description,
        "text": _limit_text(text, max_chars),
        "truncated": len(text) > max_chars,
    }


@mcp.tool()
async def extract_links(
    url: AnyHttpUrl,
    same_host_only: bool = False,
    limit: int = Field(default=50, ge=1, le=200),
) -> dict[str, Any]:
    """Extract normalized links from a page, optionally restricted to one host."""
    response, html = await _fetch_url(str(url), max_chars=MAX_RESPONSE_CHARS)
    base_url = str(response.url)
    base_host = urlparse(base_url).netloc
    soup = BeautifulSoup(html, "html.parser")

    links: list[dict[str, str]] = []
    seen: set[str] = set()

    for anchor in soup.find_all("a", href=True):
        href = urljoin(base_url, str(anchor["href"]))
        parsed = urlparse(href)
        if parsed.scheme not in {"http", "https"}:
            continue
        if same_host_only and parsed.netloc != base_host:
            continue
        if href in seen:
            continue

        seen.add(href)
        links.append({"url": href, "text": _clean_text(anchor.get_text(" "))})
        if len(links) >= limit:
            break

    return {"url": base_url, "count": len(links), "links": links}


@mcp.tool()
async def check_url_status(url: AnyHttpUrl) -> dict[str, Any]:
    """Check whether a URL is reachable and return status, timing, and headers."""
    started_at = time()
    try:
        response, _ = await _fetch_url(str(url), max_chars=2_000)
        elapsed_ms = round((time() - started_at) * 1000, 2)
        return {
            "ok": True,
            "url": str(response.url),
            "status_code": response.status_code,
            "elapsed_ms": elapsed_ms,
            "content_type": response.headers.get("content-type", ""),
            "server": response.headers.get("server", ""),
        }
    except httpx.HTTPError as exc:
        elapsed_ms = round((time() - started_at) * 1000, 2)
        return {"ok": False, "url": str(url), "elapsed_ms": elapsed_ms, "error": str(exc)}


@mcp.tool()
async def analyze_text(
    text: str = Field(min_length=1, max_length=100_000),
    top_n: int = Field(default=12, ge=1, le=50),
) -> dict[str, Any]:
    """Summarize basic text statistics and the most frequent meaningful words."""
    words = re.findall(r"[A-Za-z][A-Za-z'-]{1,}", text.lower())
    sentences = re.findall(r"[^.!?]+[.!?]?", text)
    stopwords = {
        "the",
        "and",
        "for",
        "that",
        "this",
        "with",
        "from",
        "are",
        "was",
        "were",
        "you",
        "your",
        "have",
        "has",
        "but",
        "not",
        "can",
        "will",
        "all",
        "our",
    }
    meaningful_words = [word for word in words if word not in stopwords]
    top_terms = Counter(meaningful_words).most_common(top_n)

    return {
        "characters": len(text),
        "words": len(words),
        "sentences": len([s for s in sentences if s.strip()]),
        "estimated_reading_minutes": round(len(words) / 225, 2),
        "top_terms": [{"term": term, "count": count} for term, count in top_terms],
    }


app = mcp.http_app(path="/mcp", transport="streamable-http")


def main() -> None:
    import uvicorn

    port = int(os.getenv("PORT", "8000"))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False)


if __name__ == "__main__":
    main()
