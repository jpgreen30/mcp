from __future__ import annotations

import os
import re
import secrets
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
CREWAI_WORKFLOWS = {
    "default": {
        "api_url_env": "CREWAI_API_URL",
        "token_env": "CREWAI_BEARER_TOKEN",
        "required_inputs": [],
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
    },
}
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
        }

    return routes


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

    if workflow_id == "life_insurance_research":
        if inputs.get("workflow_id") != workflow_id:
            raise ValueError("workflow_id input must be 'life_insurance_research'")

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
    return JSONResponse({"ok": True, "routes": _crewai_route_debug()})


app = mcp.http_app(path="/mcp", transport="streamable-http")


def main() -> None:
    import uvicorn

    port = int(os.getenv("PORT", "8000"))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False)


if __name__ == "__main__":
    main()
