# Cloud Tools Gateway

Remote MCP tools server built with Python, FastMCP, Streamable HTTP, and static bearer-token authentication.

## Local Development

```powershell
uv sync
$env:MCP_BEARER_TOKEN = "replace-with-a-long-random-secret"
uv run uvicorn main:app --host 0.0.0.0 --port 8000
```

MCP endpoint:

```text
http://localhost:8000/mcp
```

Clients must send:

```text
Authorization: Bearer replace-with-a-long-random-secret
```

For ChatGPT custom connectors, use OAuth authentication. The server exposes:

- Authorization metadata: `/.well-known/oauth-authorization-server`
- Authorization URL: `/oauth/authorize`
- Token URL: `/oauth/token`
- MCP resource: `/mcp`

Set `PUBLIC_BASE_URL` in production, for example `https://mcp-dh2a.onrender.com`.

## Tools

- `fetch_webpage`: fetches a URL and returns clean text plus page metadata.
- `extract_links`: extracts normalized links from a URL.
- `check_url_status`: checks URL reachability, status, timing, and headers.
- `analyze_text`: returns basic text statistics and top terms.
- `run_crewai_automation`: sends an order to a configured CrewAI deployment.
- `call_crewai_endpoint`: calls safe GET/POST paths on the configured CrewAI deployment API.
- `run_crewai_workflow`: starts the configured CrewAI workflow with `{"inputs": {...}}`.
- `get_crewai_status`: polls `GET /status/{kickoff_id}`.
- `get_crewai_result`: reads final output from the status response.

## Docker

Build:

```bash
docker build -t cloud-tools-gateway .
```

Run:

```bash
docker run --rm -p 8000:8000 -e MCP_BEARER_TOKEN="replace-with-a-long-random-secret" cloud-tools-gateway
```

## Cloud Deployment

Use these settings on Render, Railway, Fly.io, Google Cloud Run, or a similar container host:

- Build command: `docker build -t cloud-tools-gateway .`
- Run command: `uv run --frozen uvicorn main:app --host 0.0.0.0 --port $PORT`
- Required environment variable: `MCP_BEARER_TOKEN`
- Recommended environment variable: `PUBLIC_BASE_URL`
- Optional environment variable: `MCP_CLIENT_ID`
- Optional CrewAI bridge variables: `CREWAI_API_URL`, `CREWAI_BEARER_TOKEN`
- Public MCP URL: `https://<your-domain>/mcp`

For container platforms that run the `Dockerfile` directly, set only `MCP_BEARER_TOKEN`; the `CMD` is already included.

## CrewAI

CrewAI can connect to the same remote MCP endpoint with direct bearer-token headers.

Install CrewAI MCP support in your agent project:

```bash
uv add crewai
```

Set environment variables:

```bash
export MCP_URL="https://mcp-dh2a.onrender.com/mcp"
export MCP_BEARER_TOKEN="your-render-mcp-token"
```

Use `examples/crewai_remote_mcp.py` as a starting point. The key configuration is:

```python
from crewai.mcp import MCPServerHTTP

tools = MCPServerHTTP(
    url="https://mcp-dh2a.onrender.com/mcp",
    headers={"Authorization": f"Bearer {MCP_BEARER_TOKEN}"},
    cache_tools_list=True,
)
```

## ChatGPT To CrewAI Bridge

To let ChatGPT give orders to a CrewAI deployment through this MCP server, configure these environment variables on the MCP deployment:

```bash
CREWAI_API_URL="https://your-crew-deployment.crewai.com"
CREWAI_BEARER_TOKEN="your-crewai-deployment-bearer-token"
```

Optional per-workflow override for the Life Insurance Lead Crew:

```bash
CREWAI_LIFE_INSURANCE_API_URL="https://your-life-insurance-crew.crewai.com"
CREWAI_LIFE_INSURANCE_BEARER_TOKEN="your-life-insurance-crew-token"
```

If those override variables are not set, `life_insurance_leads` uses `CREWAI_API_URL` and `CREWAI_BEARER_TOKEN`.

Optional per-workflow override for the Life Insurance Research Crew:

```bash
CREWAI_LIFE_INSURANCE_RESEARCH_API_URL="https://your-life-insurance-research-crew.crewai.com"
CREWAI_LIFE_INSURANCE_RESEARCH_BEARER_TOKEN="your-life-insurance-research-crew-token"
```

If those override variables are not set, `life_insurance_research` uses `CREWAI_API_URL` and `CREWAI_BEARER_TOKEN`.

After redeploying, refresh the ChatGPT connector actions. ChatGPT will see:

- `run_crewai_automation`: starts the configured CrewAI deployment via `/kickoff`.
- `call_crewai_endpoint`: makes constrained GET/POST calls to a CrewAI deployment API. Pass `workflow_id` to inspect non-default routes.
- `run_crewai_workflow`: sends `POST /kickoff` with nested inputs, such as `{"inputs": {"user_name": "Jean"}}`.
- `get_crewai_status`: checks run state with `GET /status/{kickoff_id}`.
- `get_crewai_result`: returns the final result from `GET /status/{kickoff_id}`.

CrewAI status is the source of truth for output. This deployment returns final output in the `/status/{kickoff_id}` payload; `/output/{kickoff_id}` is not used.

Life insurance lead workflow input example:

```python
run_crewai_workflow(
    workflow_id="life_insurance_leads",
    inputs={
        "client_name": "MotherlyQuotes",
        "target_audience": "new and expecting moms",
        "licensed_states": ["CA"],
        "offer": "free life insurance quote check",
        "crm_destination": "HubSpot",
        "followup_channel": "Brevo",
    },
)
```

Life insurance research workflow input example:

```python
run_crewai_workflow(
    workflow_id="life_insurance_research",
    inputs={
        "user_name": "Jean Pierre",
        "client_name": "MotherlyQuotes",
        "target_audience": "new and expecting moms",
        "licensed_states": ["CA"],
        "product_focus": "term life insurance",
        "competitors": ["Policygenius", "Ethos", "Ladder", "SelectQuote"],
        "offer": "free life insurance quote check",
        "crm_destination": "HubSpot",
        "followup_channel": "Brevo",
        "output_format": "markdown_and_json",
    },
)
```

The gateway adds `workflow_id="life_insurance_research"` into the nested CrewAI inputs payload when the MCP workflow parameter is used.

Route debug endpoint:

```bash
curl https://mcp-dh2a.onrender.com/debug/routes
```

This returns configured CrewAI API URLs and token presence flags without exposing bearer token values.

To inspect the life insurance research deployment inputs through MCP, call:

```python
call_crewai_endpoint(
    method="GET",
    path="/inputs",
    workflow_id="life_insurance_research",
)
```
