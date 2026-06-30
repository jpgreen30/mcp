from __future__ import annotations

import os
import re
import secrets
import asyncio
import json
from collections import Counter
from html import unescape
from time import time
from typing import Any
from urllib.parse import urlencode, urljoin, urlparse

import httpx
from bs4 import BeautifulSoup
from fastmcp import FastMCP
from fastmcp.server.auth import AccessToken, TokenVerifier
from pydantic import AnyHttpUrl, Field
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response


DEFAULT_TIMEOUT_SECONDS = 12.0
DEFAULT_USER_AGENT = "Cloud-Tools-Gateway/0.1 (+https://modelcontextprotocol.io)"
MAX_RESPONSE_CHARS = 60_000
AUTH_CODE_TTL_SECONDS = 300
CREWAI_DEFAULT_PAYLOAD_FIELDS = {
    "taskWebhookUrl": "",
    "stepWebhookUrl": "",
    "crewWebhookUrl": "",
    "trainingFilename": "",
    "generateArtifact": False,
}
SUPPORTED_WORKFLOWS = {
    "default": {
        "api_url_env": "CREWAI_API_URL",
        "token_env": "CREWAI_BEARER_TOKEN",
        "required_inputs": [],
        "result_polling_supported": True,
    },
    "life_insurance_leads": {
        "api_url_env": "CREWAI_LIFE_INSURANCE_API_URL",
        "token_env": "CREWAI_LIFE_INSURANCE_BEARER_TOKEN",
        "fallback_api_url_env": "CREWAI_API_URL",
        "fallback_token_env": "CREWAI_BEARER_TOKEN",
        "required_inputs": [
            "client_name",
            "target_audience",
            "licensed_states",
            "offer",
            "crm_destination",
            "followup_channel",
        ],
        "result_polling_supported": True,
    },
    "life_insurance_research": {
        "api_url_env": "CREWAI_LIFE_INSURANCE_RESEARCH_API_URL",
        "token_env": "CREWAI_LIFE_INSURANCE_RESEARCH_BEARER_TOKEN",
        "fallback_api_url_env": "CREWAI_API_URL",
        "fallback_token_env": "CREWAI_BEARER_TOKEN",
        "required_inputs": [
            "workflow_id",
            "user_name",
            "client_name",
            "target_audience",
            "licensed_states",
            "product_focus",
            "competitors",
            "offer",
            "crm_destination",
            "followup_channel",
            "output_format",
        ],
        "result_polling_supported": True,
    },
    "life_insurance_content": {
        "local_handler": "life_insurance_content",
        "required_inputs": [
            "workflow_id",
            "user_name",
            "client_name",
            "target_audience",
            "licensed_states",
            "product_focus",
            "research_summary",
            "offer",
            "channels",
            "tone",
            "output_format",
        ],
        "result_polling_supported": True,
    },
    "life_insurance_seo": {
        "local_handler": "life_insurance_seo",
        "required_inputs": [
            "workflow_id",
            "user_name",
            "client_name",
            "target_audience",
            "licensed_states",
            "product_focus",
            "research_summary",
            "competitors",
            "output_format",
        ],
        "result_polling_supported": True,
    },
    "life_insurance_retell": {
        "local_handler": "life_insurance_retell",
        "required_inputs": [
            "workflow_id",
            "user_name",
            "client_name",
            "target_audience",
            "licensed_states",
            "product_focus",
            "research_summary",
            "offer",
            "crm_destination",
            "followup_channel",
            "output_format",
        ],
        "result_polling_supported": True,
    },
    "life_insurance_email": {
        "local_handler": "life_insurance_email",
        "required_inputs": [
            "workflow_id",
            "user_name",
            "client_name",
            "target_audience",
            "licensed_states",
            "product_focus",
            "research_summary",
            "offer",
            "crm_destination",
            "followup_channel",
            "output_format",
        ],
        "result_polling_supported": True,
    },
    "life_insurance_compliance": {
        "local_handler": "life_insurance_compliance",
        "required_inputs": [
            "workflow_id",
            "user_name",
            "client_name",
            "target_audience",
            "licensed_states",
            "product_focus",
            "content_to_review",
            "offer",
            "output_format",
        ],
        "result_polling_supported": True,
    },
}
for _workflow_name in (
    "life_insurance_competitor",
    "life_insurance_crm",
    "life_insurance_analytics",
):
    SUPPORTED_WORKFLOWS[_workflow_name] = {
        "api_url_env": f"CREWAI_{_workflow_name.upper()}_API_URL",
        "token_env": f"CREWAI_{_workflow_name.upper()}_BEARER_TOKEN",
        "fallback_api_url_env": "CREWAI_API_URL",
        "fallback_token_env": "CREWAI_BEARER_TOKEN",
        "required_inputs": [],
        "result_polling_supported": True,
    }

CREWAI_WORKFLOWS = SUPPORTED_WORKFLOWS
LOCAL_WORKFLOW_RUNS: dict[str, dict[str, Any]] = {}
CREWAI_RESULT_ENDPOINTS = (
    "/status/{kickoff_id}",
    "/result/{kickoff_id}",
    "/kickoff/{kickoff_id}",
    "/runs/{kickoff_id}",
    "/tasks/{kickoff_id}",
)
_auth_codes: dict[str, dict[str, Any]] = {}


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


def _public_base_url(request: Request) -> str:
    configured = os.getenv("PUBLIC_BASE_URL")
    if configured:
        return configured.rstrip("/")
    return str(request.base_url).rstrip("/")


def _oauth_metadata(request: Request) -> dict[str, Any]:
    base_url = _public_base_url(request)
    return {
        "issuer": base_url,
        "authorization_endpoint": f"{base_url}/oauth/authorize",
        "token_endpoint": f"{base_url}/oauth/token",
        "registration_endpoint": f"{base_url}/oauth/register",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code"],
        "code_challenge_methods_supported": ["S256", "plain"],
        "token_endpoint_auth_methods_supported": [
            "none",
            "client_secret_post",
            "client_secret_basic",
        ],
        "scopes_supported": ["tools:read", "tools:execute"],
    }


@mcp.custom_route("/health", methods=["GET"], include_in_schema=False)
async def health_check(request: Request) -> Response:
    return JSONResponse({"ok": True, "service": "Cloud-Tools-Gateway"})


@mcp.custom_route(
    "/.well-known/oauth-authorization-server",
    methods=["GET"],
    include_in_schema=False,
)
async def oauth_authorization_server_metadata(request: Request) -> Response:
    return JSONResponse(_oauth_metadata(request))


@mcp.custom_route(
    "/.well-known/oauth-authorization-server/mcp",
    methods=["GET"],
    include_in_schema=False,
)
async def oauth_authorization_server_metadata_for_mcp(request: Request) -> Response:
    return JSONResponse(_oauth_metadata(request))


@mcp.custom_route(
    "/.well-known/oauth-protected-resource",
    methods=["GET"],
    include_in_schema=False,
)
async def oauth_protected_resource_metadata(request: Request) -> Response:
    base_url = _public_base_url(request)
    return JSONResponse(
        {
            "resource": f"{base_url}/mcp",
            "authorization_servers": [base_url],
            "scopes_supported": ["tools:read", "tools:execute"],
        }
    )


@mcp.custom_route(
    "/.well-known/oauth-protected-resource/mcp",
    methods=["GET"],
    include_in_schema=False,
)
async def oauth_protected_resource_metadata_for_mcp(request: Request) -> Response:
    return await oauth_protected_resource_metadata(request)


@mcp.custom_route("/oauth/register", methods=["POST"], include_in_schema=False)
async def oauth_register(request: Request) -> Response:
    body = await request.json()
    return JSONResponse(
        {
            "client_id": body.get("client_name", "chatgpt"),
            "client_id_issued_at": int(time()),
            "redirect_uris": body.get("redirect_uris", []),
            "grant_types": ["authorization_code"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
        },
        status_code=201,
    )


@mcp.custom_route("/oauth/authorize", methods=["GET"], include_in_schema=False)
async def oauth_authorize(request: Request) -> Response:
    params = request.query_params
    redirect_uri = params.get("redirect_uri")
    if not redirect_uri:
        return JSONResponse({"error": "invalid_request", "error_description": "Missing redirect_uri"}, status_code=400)

    code = secrets.token_urlsafe(32)
    _auth_codes[code] = {
        "client_id": params.get("client_id", ""),
        "redirect_uri": redirect_uri,
        "expires_at": time() + AUTH_CODE_TTL_SECONDS,
        "scope": params.get("scope", "tools:read tools:execute"),
    }
    redirect_params = {"code": code}
    if state := params.get("state"):
        redirect_params["state"] = state
    separator = "&" if "?" in redirect_uri else "?"
    return RedirectResponse(f"{redirect_uri}{separator}{urlencode(redirect_params)}")


@mcp.custom_route("/oauth/token", methods=["POST"], include_in_schema=False)
async def oauth_token(request: Request) -> Response:
    form = await request.form()
    if form.get("grant_type") != "authorization_code":
        return JSONResponse({"error": "unsupported_grant_type"}, status_code=400)

    code = str(form.get("code", ""))
    code_record = _auth_codes.pop(code, None)
    if not code_record or code_record["expires_at"] < time():
        return JSONResponse({"error": "invalid_grant"}, status_code=400)

    redirect_uri = str(form.get("redirect_uri", ""))
    if redirect_uri and redirect_uri != code_record["redirect_uri"]:
        return JSONResponse({"error": "invalid_grant"}, status_code=400)

    token = os.getenv("MCP_BEARER_TOKEN")
    if not token:
        return JSONResponse({"error": "server_error"}, status_code=500)

    return JSONResponse(
        {
            "access_token": token,
            "token_type": "Bearer",
            "expires_in": 3600,
            "scope": code_record["scope"],
        }
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


def _crewai_config(workflow_id: str = "default") -> tuple[str, str]:
    workflow = CREWAI_WORKFLOWS.get(workflow_id)
    if workflow is None:
        supported = ", ".join(sorted(CREWAI_WORKFLOWS))
        raise ValueError(f"Unsupported workflow_id '{workflow_id}'. Supported: {supported}")
    if workflow.get("local_handler"):
        raise ValueError(f"workflow_id '{workflow_id}' is handled locally by the MCP gateway")

    api_url = os.getenv(workflow["api_url_env"], "").rstrip("/")
    token = os.getenv(workflow["token_env"], "")
    if not api_url and workflow.get("fallback_api_url_env"):
        api_url = os.getenv(str(workflow["fallback_api_url_env"]), "").rstrip("/")
    if not token and workflow.get("fallback_token_env"):
        token = os.getenv(str(workflow["fallback_token_env"]), "")

    if not api_url or not token:
        raise ValueError(
            f"CrewAI API URL and bearer token must be configured for workflow_id '{workflow_id}'"
        )
    return api_url, token


def _crewai_route_debug() -> dict[str, dict[str, Any]]:
    routes: dict[str, dict[str, Any]] = {}

    for workflow_id, workflow in CREWAI_WORKFLOWS.items():
        if workflow.get("local_handler"):
            routes[workflow_id] = {
                "api_url": None,
                "api_url_env": None,
                "fallback_api_url_env": None,
                "source": "local",
                "token_configured": True,
                "token_env": None,
                "required_inputs": workflow["required_inputs"],
                "result_polling_supported": workflow.get("result_polling_supported", False),
                "result_endpoints": ["local://workflow-runs/{kickoff_id}"],
            }
            continue

        api_url = os.getenv(workflow["api_url_env"], "").rstrip("/")
        token = os.getenv(workflow["token_env"], "")
        source = "primary"

        if not api_url and workflow.get("fallback_api_url_env"):
            api_url = os.getenv(str(workflow["fallback_api_url_env"]), "").rstrip("/")
            source = "fallback"
        if not token and workflow.get("fallback_token_env"):
            token = os.getenv(str(workflow["fallback_token_env"]), "")

        routes[workflow_id] = {
            "api_url": api_url or None,
            "api_url_env": workflow["api_url_env"],
            "fallback_api_url_env": workflow.get("fallback_api_url_env"),
            "source": source if api_url else None,
            "token_configured": bool(token),
            "token_env": workflow["token_env"],
            "required_inputs": workflow["required_inputs"],
            "result_polling_supported": workflow.get("result_polling_supported", False),
            "result_endpoints": list(CREWAI_RESULT_ENDPOINTS),
        }

    return routes


def _public_crewai_route_debug() -> dict[str, Any]:
    routes = _crewai_route_debug()
    default_route = routes.get("default", {})
    workflows = {
        workflow_id: {
            "url": route["api_url"],
            "token_configured": route["token_configured"],
            "result_polling_supported": route["result_polling_supported"],
            "required_inputs": route["required_inputs"],
            "result_endpoints": route["result_endpoints"],
        }
        for workflow_id, route in routes.items()
        if workflow_id != "default"
    }
    return {
        "health": True,
        "default": default_route.get("api_url"),
        "workflows": workflows,
    }


def _validate_workflow_inputs(workflow_id: str, inputs: dict[str, Any]) -> None:
    workflow = CREWAI_WORKFLOWS.get(workflow_id)
    if workflow is None:
        supported = ", ".join(sorted(CREWAI_WORKFLOWS))
        raise ValueError(f"Unsupported workflow_id '{workflow_id}'. Supported: {supported}")

    missing = [key for key in workflow["required_inputs"] if key not in inputs]
    if missing:
        raise ValueError(f"Missing required inputs for {workflow_id}: {', '.join(missing)}")

    if workflow_id in {"life_insurance_leads", "life_insurance_research"}:
        licensed_states = inputs.get("licensed_states")
        if not isinstance(licensed_states, list) or not licensed_states:
            raise ValueError("licensed_states must be a non-empty list")

    if workflow_id.startswith("life_insurance_") and "licensed_states" in workflow["required_inputs"]:
        licensed_states = inputs.get("licensed_states")
        if not isinstance(licensed_states, list) or not licensed_states:
            raise ValueError("licensed_states must be a non-empty list")

    if workflow_id.startswith("life_insurance_"):
        if inputs.get("workflow_id") != workflow_id:
            raise ValueError(f"workflow_id input must be '{workflow_id}'")

    if workflow_id in {"life_insurance_research", "life_insurance_seo"}:
        competitors = inputs.get("competitors")
        if not isinstance(competitors, list) or not competitors:
            raise ValueError("competitors must be a non-empty list")


async def _crewai_request(
    method: str,
    path: str,
    *,
    json_body: dict[str, Any] | None = None,
    timeout_seconds: float = 120.0,
    workflow_id: str = "default",
) -> dict[str, Any]:
    api_url, token = _crewai_config(workflow_id)
    normalized_path = "/" + path.lstrip("/")
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    async with httpx.AsyncClient(timeout=httpx.Timeout(timeout_seconds)) as client:
        response = await client.request(
            method.upper(),
            f"{api_url}{normalized_path}",
            headers=headers,
            json=json_body,
        )

    content_type = response.headers.get("content-type", "")
    try:
        data: Any = response.json() if "json" in content_type else response.text
    except ValueError:
        data = response.text

    return {
        "ok": response.is_success,
        "status_code": response.status_code,
        "url": str(response.url),
        "data": data,
    }


def _extract_kickoff_id(response: dict[str, Any]) -> str | None:
    data = response.get("data")
    if isinstance(data, dict):
        value = data.get("kickoff_id") or data.get("kickoff_uuid") or data.get("id")
        return str(value) if value else None
    return None


def _extract_status_result(status_response: dict[str, Any]) -> dict[str, Any]:
    data = status_response.get("data")
    if not isinstance(data, dict):
        return {"state": None, "status": None, "result": None, "raw": status_response}

    result = data.get("result")
    if result is None:
        result = data.get("result_json")
    if result is None and isinstance(data.get("last_executed_task"), dict):
        result = data["last_executed_task"].get("output")
    if result is None and isinstance(data.get("last_step"), dict):
        result = data["last_step"].get("result") or data["last_step"].get("prompt")

    return {
        "state": data.get("state"),
        "status": data.get("status"),
        "result": result,
        "result_json": data.get("result_json"),
        "last_executed_task": data.get("last_executed_task"),
        "raw": data,
    }


def _normalize_state(value: Any) -> str:
    state = str(value or "").strip().lower()
    if state in {"success", "successful", "completed", "complete", "done"}:
        return "completed"
    if state in {"failed", "failure", "error", "errored", "timed_out", "timeout"}:
        return "failed"
    if state in {"pending", "queued", "running", "in_progress", "started"}:
        return "running"
    return state or "unknown"


def _extract_json_from_markdown(markdown: str) -> Any | None:
    fenced_matches = re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", markdown, flags=re.DOTALL | re.IGNORECASE)
    for match in fenced_matches:
        try:
            return json.loads(match)
        except json.JSONDecodeError:
            continue
    return None


def _format_workflow_result(
    *,
    workflow_id: str,
    kickoff_id: str,
    endpoint: str | None,
    status_response: dict[str, Any],
) -> dict[str, Any]:
    parsed = _extract_status_result(status_response)
    state = _normalize_state(parsed.get("state") or parsed.get("status"))
    result = parsed.get("result")
    result_json = parsed.get("result_json")
    markdown_report = result if isinstance(result, str) else None

    if result_json is None and isinstance(result, dict):
        result_json = result
    if result_json is None and markdown_report:
        result_json = _extract_json_from_markdown(markdown_report)

    return {
        "ok": status_response.get("ok", False) and state != "failed",
        "workflow_id": workflow_id,
        "kickoff_id": kickoff_id,
        "status": state,
        "result_endpoint": endpoint,
        "result": result_json if result_json is not None else result,
        "markdown_report": markdown_report,
        "raw_result": result,
        "status_response": status_response,
    }


def _states_text(states: Any) -> str:
    if isinstance(states, list):
        return ", ".join(str(state) for state in states)
    return str(states or "")


def _listify(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value]
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    return []


def _summary_text(value: Any, max_chars: int = 1600) -> str:
    if isinstance(value, str):
        return _limit_text(value, max_chars)
    return _limit_text(json.dumps(value, ensure_ascii=False), max_chars)


def _markdown_section(title: str, items: list[str]) -> str:
    lines = [f"## {title}"]
    lines.extend(f"- {item}" for item in items)
    return "\n".join(lines)


def _campaign_safety_notes(states: Any) -> list[str]:
    states_text = _states_text(states)
    return [
        "Do not state or imply guaranteed approval, guaranteed savings, guaranteed premiums, or guaranteed coverage.",
        "Keep copy educational and lead-generation focused; route product-specific advice to a licensed professional.",
        f"Limit active campaign targeting to licensed states only: {states_text}.",
        "Use terms like 'explore options', 'request a quote check', and 'learn what may fit your family' instead of promises.",
    ]


def _content_workflow(inputs: dict[str, Any]) -> dict[str, Any]:
    client = inputs["client_name"]
    audience = inputs["target_audience"]
    product = inputs["product_focus"]
    offer = inputs["offer"]
    tone = inputs["tone"]
    channels = _listify(inputs["channels"]) or ["Facebook", "TikTok", "Email", "Landing page", "SMS"]
    states = inputs["licensed_states"]
    summary = _summary_text(inputs["research_summary"])

    facebook_ads = [
        {
            "primary_text": f"New or expecting a child? {client} can help you explore {product} options with a {offer}.",
            "headline": "Protect what matters most",
            "cta": "Request quote check",
            "compliance_note": "Avoid claims about guaranteed coverage or savings.",
        },
        {
            "primary_text": f"Life insurance can feel complicated. {client} makes the first step simpler for {audience}.",
            "headline": "A simpler first step",
            "cta": "Start free check",
            "compliance_note": "Position as education and quote exploration only.",
        },
    ]
    tiktok_scripts = [
        {
            "hook": "Three life insurance questions new parents should ask before the baby arrives.",
            "beats": [
                "Name the emotional moment: growing family, new responsibilities.",
                f"Explain the {offer} as a no-pressure starting point.",
                "Invite viewers to compare options with a licensed follow-up.",
            ],
            "cta": "Tap to request your free quote check.",
        }
    ]
    email_sequence = [
        {"day": 0, "subject": "Your free life insurance quote check", "body": f"Thanks for reaching out to {client}. Here is what to expect next: a simple review of options available in {_states_text(states)}."},
        {"day": 2, "subject": "What new parents often overlook", "body": f"Many {audience} compare {product} after a major family milestone. Here are questions to bring to a licensed advisor."},
        {"day": 5, "subject": "Ready to compare options?", "body": f"Your {offer} is still available. No guarantees, just a clear next step to explore coverage options."},
    ]
    landing_page_copy = {
        "hero_headline": f"Life insurance quote checks for {audience}",
        "subheadline": f"Explore {product} options in {_states_text(states)} with a simple, educational first step.",
        "form_cta": "Get my free quote check",
        "trust_copy": "Licensed follow-up. Clear information. No pressure.",
        "faq": [
            "Will this guarantee coverage? No. Eligibility and pricing depend on carrier and underwriting review.",
            "Is this financial advice? No. This is lead-generation and educational content only.",
        ],
    }
    sms_followups = [
        f"Hi from {client}. Your {offer} request is ready. Reply YES to choose a time to review options.",
        "Quick reminder: your quote check is educational and does not guarantee coverage or pricing. Want help comparing next steps?",
    ]
    compliance_notes = _campaign_safety_notes(states)
    markdown_report = "\n\n".join([
        f"# Compliant Content Package for {client}",
        f"Tone: {tone}\nChannels: {', '.join(channels)}\n\nResearch basis: {summary}",
        _markdown_section("Facebook Ads", [ad["headline"] + ": " + ad["primary_text"] for ad in facebook_ads]),
        _markdown_section("TikTok Scripts", [tiktok_scripts[0]["hook"]]),
        _markdown_section("Compliance Notes", compliance_notes),
    ])
    return {
        "facebook_ads": facebook_ads,
        "tiktok_scripts": tiktok_scripts,
        "email_sequence": email_sequence,
        "landing_page_copy": landing_page_copy,
        "sms_followups": sms_followups,
        "compliance_notes": compliance_notes,
        "markdown_report": markdown_report,
    }


def _seo_workflow(inputs: dict[str, Any]) -> dict[str, Any]:
    client = inputs["client_name"]
    audience = inputs["target_audience"]
    product = inputs["product_focus"]
    states = inputs["licensed_states"]
    competitors = _listify(inputs["competitors"])
    state_slug = _states_text(states)
    keyword_clusters = [
        {"cluster": "new parent life insurance", "keywords": ["life insurance for new parents", "term life insurance for moms", "life insurance after having a baby"]},
        {"cluster": "California quote intent", "keywords": ["term life insurance quotes California", "life insurance quote check CA", "affordable term life insurance California"]},
        {"cluster": "education and objections", "keywords": ["how much life insurance do parents need", "life insurance myths for moms", "term vs whole life for parents"]},
    ]
    blog_topics = [
        f"Life insurance questions {audience} should ask before comparing quotes",
        f"How {product} works for growing families in {state_slug}",
        "What a quote check can and cannot tell you",
    ]
    landing_page_topics = [
        f"{product.title()} quote check for {audience}",
        f"Life insurance education hub for California parents",
    ]
    local_seo_opportunities = [
        f"Build localized pages for licensed state(s): {state_slug}.",
        "Add local FAQ schema around quote process, licensed follow-up, and no-guarantee disclaimers.",
    ]
    competitor_seo_gaps = [
        f"{competitor}: create parent-specific comparison content that avoids product-performance claims."
        for competitor in competitors
    ]
    content_calendar = [
        {"week": 1, "asset": blog_topics[0], "goal": "Capture educational search intent"},
        {"week": 2, "asset": landing_page_topics[0], "goal": "Convert quote-check traffic"},
        {"week": 3, "asset": blog_topics[1], "goal": "Support local SEO"},
        {"week": 4, "asset": blog_topics[2], "goal": "Reduce objections"},
    ]
    markdown_report = "\n\n".join([
        f"# SEO Opportunity Plan for {client}",
        _markdown_section("Keyword Clusters", [cluster["cluster"] for cluster in keyword_clusters]),
        _markdown_section("Blog Topics", blog_topics),
        _markdown_section("Competitor SEO Gaps", competitor_seo_gaps),
    ])
    return {
        "keyword_clusters": keyword_clusters,
        "blog_topics": blog_topics,
        "landing_page_topics": landing_page_topics,
        "local_seo_opportunities": local_seo_opportunities,
        "competitor_seo_gaps": competitor_seo_gaps,
        "content_calendar": content_calendar,
        "markdown_report": markdown_report,
    }


def _retell_workflow(inputs: dict[str, Any]) -> dict[str, Any]:
    client = inputs["client_name"]
    audience = inputs["target_audience"]
    product = inputs["product_focus"]
    offer = inputs["offer"]
    states = inputs["licensed_states"]
    opening_script = f"Hi, this is the {client} assistant. I’m following up about your {offer}. I can ask a few basic questions to help route you to a licensed person for {product} options in {_states_text(states)}."
    qualification_questions = [
        "Are you currently located in one of our licensed states?",
        "Are you exploring coverage for yourself, a spouse or partner, or family protection planning?",
        "Are you interested in term coverage, or are you still comparing product types?",
        "What is the best time for a licensed follow-up?",
    ]
    objection_responses = {
        "too_expensive": "That is a common concern. I cannot quote or guarantee pricing, but a licensed follow-up can help you compare available options.",
        "already_have_policy": "That is helpful. This can still be a quote check to compare whether your current setup still fits your family goals.",
        "not_ready": "No problem. I can send educational information and schedule a later follow-up.",
    }
    appointment_booking_flow = [
        "Confirm licensed state.",
        "Confirm preferred contact method.",
        "Offer two appointment windows.",
        "Send confirmation to CRM and follow-up channel.",
    ]
    compliance_disclaimers = _campaign_safety_notes(states)
    handoff_rules = [
        "Transfer or create task for licensed agent when user asks for coverage recommendation.",
        "Stop qualification if user asks for legal, tax, financial, or underwriting advice.",
        "Flag urgent complaints or cancellation requests for human review.",
    ]
    crm_fields_to_capture = ["first_name", "last_name", "phone", "email", "state", "coverage_interest", "preferred_callback_time", "consent_to_contact"]
    markdown_report = "\n\n".join([
        f"# Retell Voice Agent Script for {client}",
        opening_script,
        _markdown_section("Qualification Questions", qualification_questions),
        _markdown_section("Compliance Disclaimers", compliance_disclaimers),
    ])
    return {
        "opening_script": opening_script,
        "qualification_questions": qualification_questions,
        "objection_responses": objection_responses,
        "appointment_booking_flow": appointment_booking_flow,
        "compliance_disclaimers": compliance_disclaimers,
        "handoff_rules": handoff_rules,
        "crm_fields_to_capture": crm_fields_to_capture,
        "markdown_report": markdown_report,
    }


def _email_workflow(inputs: dict[str, Any]) -> dict[str, Any]:
    client = inputs["client_name"]
    audience = inputs["target_audience"]
    offer = inputs["offer"]
    crm = inputs["crm_destination"]
    followup = inputs["followup_channel"]
    sequence = [
        {"day": 0, "subject": "Your quote check request", "body": f"Thanks for requesting a {offer} from {client}. Here is what happens next."},
        {"day": 1, "subject": "A simple first step for new parents", "body": f"Many {audience} start with a quote check to understand available options."},
        {"day": 2, "subject": "Common life insurance questions", "body": "Here are educational questions to ask before comparing options."},
        {"day": 3, "subject": "What affects eligibility and pricing?", "body": "Carrier review and underwriting can affect eligibility and pricing; no outcomes are guaranteed."},
        {"day": 5, "subject": "Want help reviewing options?", "body": "A licensed follow-up can answer product-specific questions."},
        {"day": 7, "subject": "Still interested in your free check?", "body": "Your request is still open if you want to continue."},
        {"day": 10, "subject": "Final reminder: compare options clearly", "body": "Use your quote check as an educational next step, not a coverage guarantee."},
    ]
    subject_lines = [item["subject"] for item in sequence]
    sms_followups = [
        f"{client}: Your {offer} request is ready. Reply YES for a licensed follow-up.",
        f"{client}: Reminder - no coverage or savings are guaranteed. Want to compare next steps?",
    ]
    segmentation_rules = [
        "Segment by licensed state before routing.",
        "Segment by parent/newborn interest for family-protection messaging.",
        "Suppress users without consent to contact.",
    ]
    nurture_logic = [
        f"Create lifecycle stage in {crm}: New Quote Check Lead.",
        f"Use {followup} for short reminder touches only after consent.",
        "Escalate product-specific replies to licensed agent.",
    ]
    compliance_notes = _campaign_safety_notes(inputs["licensed_states"])
    markdown_report = "\n\n".join([
        f"# Follow-Up Campaign for {client}",
        _markdown_section("Subject Lines", subject_lines),
        _markdown_section("Segmentation Rules", segmentation_rules),
        _markdown_section("Compliance Notes", compliance_notes),
    ])
    return {
        "7_day_email_sequence": sequence,
        "subject_lines": subject_lines,
        "sms_followups": sms_followups,
        "segmentation_rules": segmentation_rules,
        "nurture_logic": nurture_logic,
        "compliance_notes": compliance_notes,
        "markdown_report": markdown_report,
    }


def _compliance_workflow(inputs: dict[str, Any]) -> dict[str, Any]:
    text = _summary_text(inputs["content_to_review"], max_chars=10_000)
    risky_patterns = [
        ("guaranteed approval", "Do not claim guaranteed approval."),
        ("guaranteed savings", "Do not claim guaranteed savings."),
        ("guaranteed coverage", "Do not claim guaranteed coverage."),
        ("best rate", "Avoid superlative pricing claims unless substantiated and approved."),
        ("no medical exam guaranteed", "Avoid implying underwriting outcomes are guaranteed."),
    ]
    flagged_claims = [
        {"claim": pattern, "reason": reason}
        for pattern, reason in risky_patterns
        if pattern in text.lower()
    ]
    safer_rewrites = [
        {"original": item["claim"], "rewrite": "Explore available options with a licensed follow-up. Eligibility and pricing are not guaranteed."}
        for item in flagged_claims
    ]
    missing_disclaimers = []
    if "not guaranteed" not in text.lower() and "no guarantee" not in text.lower():
        missing_disclaimers.append("Add a clear disclaimer that coverage, pricing, and savings are not guaranteed.")
    if "licensed" not in text.lower():
        missing_disclaimers.append("Clarify that product-specific questions should be handled by a licensed professional.")
    state_specific_notes = [
        f"Use campaigns only in licensed states: {_states_text(inputs['licensed_states'])}.",
        "Confirm state-specific advertising requirements before launch.",
    ]
    risk_score = min(100, 20 + len(flagged_claims) * 20 + len(missing_disclaimers) * 10)
    approval_status = "approved_with_notes" if risk_score < 50 else "needs_revision"
    markdown_report = "\n\n".join([
        "# Compliance Review",
        f"Risk score: {risk_score}\nApproval status: {approval_status}",
        _markdown_section("Flagged Claims", [item["claim"] for item in flagged_claims] or ["No high-risk guarantee claims detected."]),
        _markdown_section("Missing Disclaimers", missing_disclaimers or ["Core disclaimers present or not required from reviewed text."]),
    ])
    return {
        "risk_score": risk_score,
        "flagged_claims": flagged_claims,
        "safer_rewrites": safer_rewrites,
        "missing_disclaimers": missing_disclaimers,
        "state_specific_notes": state_specific_notes,
        "approval_status": approval_status,
        "markdown_report": markdown_report,
    }


LOCAL_WORKFLOW_HANDLERS = {
    "life_insurance_content": _content_workflow,
    "life_insurance_seo": _seo_workflow,
    "life_insurance_retell": _retell_workflow,
    "life_insurance_email": _email_workflow,
    "life_insurance_compliance": _compliance_workflow,
}


def _run_local_workflow(workflow_id: str, inputs: dict[str, Any]) -> dict[str, Any]:
    handler = LOCAL_WORKFLOW_HANDLERS[workflow_id]
    result = handler(inputs)
    kickoff_id = f"local-{workflow_id}-{secrets.token_urlsafe(12)}"
    run = {
        "ok": True,
        "workflow_id": workflow_id,
        "kickoff_id": kickoff_id,
        "status": "completed",
        "result_endpoint": "local://workflow-runs/{kickoff_id}",
        "result": result,
        "markdown_report": result.get("markdown_report"),
    }
    LOCAL_WORKFLOW_RUNS[kickoff_id] = run
    return run


async def _get_crewai_workflow_result_response(
    workflow_id: str,
    kickoff_id: str,
) -> dict[str, Any]:
    last_response: dict[str, Any] | None = None

    for endpoint_template in CREWAI_RESULT_ENDPOINTS:
        endpoint = endpoint_template.format(kickoff_id=kickoff_id)
        response = await _crewai_request("GET", endpoint, workflow_id=workflow_id)
        last_response = response
        if not response["ok"]:
            if response["status_code"] in {404, 405}:
                continue
            return _format_workflow_result(
                workflow_id=workflow_id,
                kickoff_id=kickoff_id,
                endpoint=endpoint,
                status_response=response,
            )

        formatted = _format_workflow_result(
            workflow_id=workflow_id,
            kickoff_id=kickoff_id,
            endpoint=endpoint,
            status_response=response,
        )
        if formatted["status"] != "unknown" or formatted["result"] is not None:
            return formatted

    return {
        "ok": False,
        "workflow_id": workflow_id,
        "kickoff_id": kickoff_id,
        "status": "failed",
        "result_endpoint": None,
        "result": None,
        "markdown_report": None,
        "error": "No supported CrewAI result endpoint returned a usable response.",
        "last_response": last_response,
    }


@mcp.tool()
async def run_crewai_automation(
    instructions: str = Field(
        min_length=1,
        max_length=20_000,
        description="Natural-language order or task for the CrewAI automation.",
    ),
    extra_inputs: dict[str, Any] | None = None,
    generate_artifact: bool = False,
) -> dict[str, Any]:
    """Start the configured CrewAI automation through its deployed /kickoff API."""
    inputs = {"instructions": instructions}
    if extra_inputs:
        inputs.update(extra_inputs)

    payload = {
        "inputs": inputs,
        **CREWAI_DEFAULT_PAYLOAD_FIELDS,
        "generateArtifact": generate_artifact,
    }
    return await _crewai_request("POST", "/kickoff", json_body=payload)


@mcp.tool()
async def run_crewai_workflow(
    inputs: dict[str, Any] = Field(
        description="CrewAI inputs payload, for example {'user_name': 'Jean'}.",
    ),
    workflow_id: str = Field(
        default="default",
        min_length=1,
        max_length=100,
        description="Logical CrewAI workflow id, such as 'default' or 'life_insurance_leads'.",
    ),
) -> dict[str, Any]:
    """Run a CrewAI workflow using POST /kickoff with {'inputs': {...}}."""
    prepared_inputs = dict(inputs)
    if workflow_id != "default":
        prepared_inputs.setdefault("workflow_id", workflow_id)

    _validate_workflow_inputs(workflow_id, prepared_inputs)

    if CREWAI_WORKFLOWS[workflow_id].get("local_handler"):
        run = _run_local_workflow(workflow_id, prepared_inputs)
        return {
            "ok": True,
            "workflow_id": workflow_id,
            "kickoff_id": run["kickoff_id"],
            "input_spec": {"inputs": CREWAI_WORKFLOWS[workflow_id]["required_inputs"]},
            "kickoff": {
                "ok": True,
                "status_code": 200,
                "url": run["result_endpoint"],
                "data": {
                    "kickoff_id": run["kickoff_id"],
                    "status": "completed",
                },
            },
        }

    input_spec = await _crewai_request("GET", "/inputs", workflow_id=workflow_id)
    kickoff = await _crewai_request(
        "POST",
        "/kickoff",
        json_body={"inputs": prepared_inputs},
        workflow_id=workflow_id,
    )
    kickoff_id = _extract_kickoff_id(kickoff)

    return {
        "ok": kickoff["ok"],
        "workflow_id": workflow_id,
        "kickoff_id": kickoff_id,
        "input_spec": input_spec.get("data"),
        "kickoff": kickoff,
    }


@mcp.tool()
async def run_crewai_workflow_and_wait(
    inputs: dict[str, Any] = Field(
        description="CrewAI inputs payload, for example {'user_name': 'Jean'}.",
    ),
    workflow_id: str = Field(
        default="default",
        min_length=1,
        max_length=100,
        description="Logical CrewAI workflow id, such as 'life_insurance_research'.",
    ),
    timeout_seconds: int = Field(default=180, ge=10, le=600),
    poll_interval_seconds: int = Field(default=5, ge=2, le=30),
) -> dict[str, Any]:
    """Start a CrewAI workflow, poll for completion, and return the final result."""
    kickoff_result = await run_crewai_workflow(inputs=inputs, workflow_id=workflow_id)
    kickoff_id = kickoff_result.get("kickoff_id")
    if not kickoff_result.get("ok") or not kickoff_id:
        return {
            "ok": False,
            "workflow_id": workflow_id,
            "kickoff_id": kickoff_id,
            "status": "failed",
            "error": "CrewAI workflow kickoff failed.",
            "kickoff": kickoff_result,
        }

    if kickoff_id in LOCAL_WORKFLOW_RUNS:
        run = LOCAL_WORKFLOW_RUNS[kickoff_id]
        return {
            "ok": True,
            "workflow_id": workflow_id,
            "kickoff_id": kickoff_id,
            "status": "completed",
            "result_endpoint": run["result_endpoint"],
            "result": run["result"],
            "markdown_report": run["markdown_report"],
            "raw_result": run["result"],
        }

    deadline = time() + timeout_seconds
    last_result: dict[str, Any] | None = None
    while time() < deadline:
        last_result = await _get_crewai_workflow_result_response(workflow_id, kickoff_id)
        if last_result["status"] == "completed" and last_result.get("result") is not None:
            return {
                "ok": True,
                "workflow_id": workflow_id,
                "kickoff_id": kickoff_id,
                "status": "completed",
                "result_endpoint": last_result.get("result_endpoint"),
                "result": last_result.get("result"),
                "markdown_report": last_result.get("markdown_report"),
                "raw_result": last_result.get("raw_result"),
            }
        if last_result["status"] == "failed":
            return {
                "ok": False,
                "workflow_id": workflow_id,
                "kickoff_id": kickoff_id,
                "status": "failed",
                "error": last_result.get("error") or last_result.get("raw_result") or last_result.get("status_response"),
                "result_endpoint": last_result.get("result_endpoint"),
            }
        await asyncio.sleep(poll_interval_seconds)

    return {
        "ok": False,
        "workflow_id": workflow_id,
        "kickoff_id": kickoff_id,
        "status": "timeout",
        "message": "Workflow started but result was not ready before timeout.",
        "last_result": last_result,
    }


@mcp.tool()
async def get_crewai_status(
    kickoff_id: str = Field(min_length=1, max_length=100),
    workflow_id: str = Field(default="default", min_length=1, max_length=100),
) -> dict[str, Any]:
    """Get CrewAI workflow status from GET /status/{kickoff_id}."""
    return await _crewai_request("GET", f"/status/{kickoff_id}", workflow_id=workflow_id)


@mcp.tool()
async def get_crewai_result(
    kickoff_id: str = Field(min_length=1, max_length=100),
    workflow_id: str = Field(default="default", min_length=1, max_length=100),
) -> dict[str, Any]:
    """Read the final CrewAI result from GET /status/{kickoff_id}."""
    status_response = await get_crewai_status(kickoff_id, workflow_id=workflow_id)
    parsed = _extract_status_result(status_response)
    return {
        "ok": status_response["ok"],
        "kickoff_id": kickoff_id,
        "state": parsed["state"],
        "status": parsed["status"],
        "result": parsed["result"],
        "result_json": parsed["result_json"],
        "last_executed_task": parsed["last_executed_task"],
        "status_response": status_response,
    }


@mcp.tool()
async def get_crewai_workflow_result(
    kickoff_id: str = Field(min_length=1, max_length=100),
    workflow_id: str = Field(default="default", min_length=1, max_length=100),
) -> dict[str, Any]:
    """Fetch a CrewAI workflow result from the configured workflow route."""
    if kickoff_id in LOCAL_WORKFLOW_RUNS:
        return LOCAL_WORKFLOW_RUNS[kickoff_id]
    return await _get_crewai_workflow_result_response(workflow_id, kickoff_id)


@mcp.tool()
async def create_life_insurance_campaign_package(
    user_name: str = Field(min_length=1, max_length=200),
    client_name: str = Field(min_length=1, max_length=200),
    target_audience: str = Field(min_length=1, max_length=500),
    licensed_states: list[str] = Field(min_length=1),
    product_focus: str = Field(default="term life insurance", min_length=1, max_length=300),
    competitors: list[str] = Field(default_factory=lambda: ["Policygenius", "Ethos", "Ladder", "SelectQuote"]),
    offer: str = Field(default="free life insurance quote check", min_length=1, max_length=500),
    crm_destination: str = Field(default="HubSpot", min_length=1, max_length=200),
    followup_channel: str = Field(default="Brevo", min_length=1, max_length=200),
    channels: list[str] = Field(default_factory=lambda: ["Facebook", "TikTok", "Email", "Landing Page", "SMS"]),
    tone: str = Field(default="warm, clear, compliant, parent-focused", min_length=1, max_length=300),
    output_format: str = Field(default="markdown_and_json", min_length=1, max_length=100),
    timeout_seconds: int = Field(default=300, ge=60, le=600),
) -> dict[str, Any]:
    """Create a full compliant life insurance campaign package from research through compliance review."""
    research_inputs = {
        "workflow_id": "life_insurance_research",
        "user_name": user_name,
        "client_name": client_name,
        "target_audience": target_audience,
        "licensed_states": licensed_states,
        "product_focus": product_focus,
        "competitors": competitors,
        "offer": offer,
        "crm_destination": crm_destination,
        "followup_channel": followup_channel,
        "output_format": output_format,
    }
    research = await run_crewai_workflow_and_wait(
        inputs=research_inputs,
        workflow_id="life_insurance_research",
        timeout_seconds=timeout_seconds,
        poll_interval_seconds=5,
    )
    if not research.get("ok"):
        return {
            "ok": False,
            "status": "failed",
            "failed_workflow": "life_insurance_research",
            "research": research,
        }

    research_summary = research.get("result") or research.get("markdown_report") or research.get("raw_result")
    shared = {
        "user_name": user_name,
        "client_name": client_name,
        "target_audience": target_audience,
        "licensed_states": licensed_states,
        "product_focus": product_focus,
        "research_summary": research_summary,
        "offer": offer,
        "crm_destination": crm_destination,
        "followup_channel": followup_channel,
        "output_format": output_format,
    }

    content = await run_crewai_workflow_and_wait(
        workflow_id="life_insurance_content",
        inputs={
            **shared,
            "workflow_id": "life_insurance_content",
            "channels": channels,
            "tone": tone,
        },
        timeout_seconds=60,
        poll_interval_seconds=2,
    )
    retell = await run_crewai_workflow_and_wait(
        workflow_id="life_insurance_retell",
        inputs={**shared, "workflow_id": "life_insurance_retell"},
        timeout_seconds=60,
        poll_interval_seconds=2,
    )
    email = await run_crewai_workflow_and_wait(
        workflow_id="life_insurance_email",
        inputs={**shared, "workflow_id": "life_insurance_email"},
        timeout_seconds=60,
        poll_interval_seconds=2,
    )

    content_to_review = {
        "research": research.get("result"),
        "content": content.get("result"),
        "retell": retell.get("result"),
        "email": email.get("result"),
    }
    compliance = await run_crewai_workflow_and_wait(
        workflow_id="life_insurance_compliance",
        inputs={
            "workflow_id": "life_insurance_compliance",
            "user_name": user_name,
            "client_name": client_name,
            "target_audience": target_audience,
            "licensed_states": licensed_states,
            "product_focus": product_focus,
            "content_to_review": content_to_review,
            "offer": offer,
            "output_format": output_format,
        },
        timeout_seconds=60,
        poll_interval_seconds=2,
    )

    workflow_results = {
        "life_insurance_research": research,
        "life_insurance_content": content,
        "life_insurance_retell": retell,
        "life_insurance_email": email,
        "life_insurance_compliance": compliance,
    }
    failed = [name for name, result in workflow_results.items() if not result.get("ok")]
    structured = {
        "client_name": client_name,
        "target_audience": target_audience,
        "licensed_states": licensed_states,
        "product_focus": product_focus,
        "offer": offer,
        "research": research.get("result"),
        "content": content.get("result"),
        "retell": retell.get("result"),
        "email": email.get("result"),
        "compliance": compliance.get("result"),
        "kickoff_ids": {
            name: result.get("kickoff_id")
            for name, result in workflow_results.items()
        },
    }
    markdown_parts = [
        f"# AI Marketing Campaign Package for {client_name}",
        f"Audience: {target_audience}\nLicensed states: {_states_text(licensed_states)}\nOffer: {offer}",
        "## Research",
        research.get("markdown_report") or _summary_text(research.get("result"), 3000),
        "## Content",
        content.get("markdown_report") or _summary_text(content.get("result"), 3000),
        "## Retell Voice Agent",
        retell.get("markdown_report") or _summary_text(retell.get("result"), 3000),
        "## Email and SMS Follow-Up",
        email.get("markdown_report") or _summary_text(email.get("result"), 3000),
        "## Compliance Review",
        compliance.get("markdown_report") or _summary_text(compliance.get("result"), 3000),
    ]
    return {
        "ok": not failed,
        "status": "completed" if not failed else "partial_failure",
        "failed_workflows": failed,
        "workflow_results": workflow_results,
        "result": structured,
        "markdown_report": "\n\n".join(markdown_parts),
    }


@mcp.tool()
async def call_crewai_endpoint(
    method: str = Field(pattern="^(GET|POST)$"),
    path: str = Field(
        min_length=1,
        max_length=200,
        description="CrewAI deployment API path, such as /kickoff or /inputs.",
    ),
    json_body: dict[str, Any] | None = None,
    workflow_id: str = Field(
        default="default",
        min_length=1,
        max_length=100,
        description="Logical CrewAI workflow id used to select the deployment route.",
    ),
) -> dict[str, Any]:
    """Call a safe GET or POST path on the configured CrewAI deployment API."""
    if not path.startswith("/"):
        path = f"/{path}"
    blocked_fragments = {"..", "reset", "delete", "settings"}
    if any(fragment in path.lower() for fragment in blocked_fragments):
        raise ValueError("This CrewAI endpoint path is blocked by the MCP gateway")
    return await _crewai_request(method, path, json_body=json_body, workflow_id=workflow_id)


@mcp.custom_route("/debug/routes", methods=["GET"], include_in_schema=False)
async def debug_routes(request: Request) -> Response:
    debug = _public_crewai_route_debug()
    debug["routes"] = _crewai_route_debug()
    return JSONResponse(debug)


app = mcp.http_app(path="/mcp", transport="streamable-http")


def main() -> None:
    import uvicorn

    port = int(os.getenv("PORT", "8000"))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False)


if __name__ == "__main__":
    main()
