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
- `run_crewai_workflow_and_wait`: starts a CrewAI workflow, polls until completion, and returns the finished report.
- `get_crewai_status`: polls `GET /status/{kickoff_id}`.
- `get_crewai_result`: reads final output from the status response.
- `get_crewai_workflow_result`: fetches a completed workflow result later by `workflow_id` and `kickoff_id`.
- `run_ping_os`: the stable ChatGPT-visible command interface for Ping OS objectives, debug, and run retrieval.
- `create_life_insurance_campaign_package`: runs the Life Insurance Marketing OS sequence and returns one combined compliant campaign package.
- `run_ping_os_objective`: lets ChatGPT give Ping OS a business objective; the supervisor plans and runs the needed workflows.
- `get_ping_os_run`: fetches a stored Ping OS supervisor run by `run_id`.

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

The downstream Life Insurance Marketing OS workflows are available through the same MCP tools:

- `life_insurance_content`
- `life_insurance_seo`
- `life_insurance_retell`
- `life_insurance_email`
- `life_insurance_compliance`

These currently run as MCP Gateway workflow handlers, so they do not need separate CrewAI Cloud deployments. Dedicated CrewAI deployments can be added later by setting each workflow's env vars and replacing the local handler.

After redeploying, refresh the ChatGPT connector actions. ChatGPT will see:

- `run_crewai_automation`: starts the configured CrewAI deployment via `/kickoff`.
- `call_crewai_endpoint`: makes constrained GET/POST calls to a CrewAI deployment API. Pass `workflow_id` to inspect non-default routes.
- `run_crewai_workflow`: sends `POST /kickoff` with nested inputs, such as `{"inputs": {"user_name": "Jean"}}`.
- `run_crewai_workflow_and_wait`: sends `POST /kickoff`, polls result endpoints, and returns the final JSON plus markdown report.
- `get_crewai_status`: checks run state with `GET /status/{kickoff_id}`.
- `get_crewai_result`: returns the final result from `GET /status/{kickoff_id}`.
- `get_crewai_workflow_result`: fetches final output later using the workflow route.
- `run_ping_os`: the preferred permanent interface. ChatGPT sends one business objective and Ping OS handles routing internally.
- `create_life_insurance_campaign_package`: creates a full MotherlyQuotes-style campaign package by chaining research, content, Retell, email, and compliance workflows.
- `run_ping_os_objective`: accepts a plain-English business objective, selects a plan, runs workflows, and returns one strategy package.
- `get_ping_os_run`: retrieves the stored supervisor run record, final JSON, and markdown report.

Going forward, ChatGPT should depend on `run_ping_os` instead of a growing list of workflow-specific tools. Older tools remain for compatibility, diagnostics, and direct workflow testing.

CrewAI status is the source of truth for output. This deployment returns final output in the `/status/{kickoff_id}` payload. The gateway also probes `/result/{kickoff_id}`, `/kickoff/{kickoff_id}`, `/runs/{kickoff_id}`, and `/tasks/{kickoff_id}` as fallbacks.

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
run_crewai_workflow_and_wait(
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
    timeout_seconds=180,
    poll_interval_seconds=5,
)
```

The gateway adds `workflow_id="life_insurance_research"` into the nested CrewAI inputs payload when the MCP workflow parameter is used.

Route debug endpoint:

```bash
curl https://mcp-dh2a.onrender.com/debug/routes
```

This returns configured CrewAI API URLs and token presence flags without exposing bearer token values.

Ping OS supervisor debug endpoint:

```bash
curl https://mcp-dh2a.onrender.com/debug/ping-os
```

This returns supervisor health, supported verticals, supported objective types, available workflows, and current in-process run count.

To inspect the life insurance research deployment inputs through MCP, call:

```python
call_crewai_endpoint(
    method="GET",
    path="/inputs",
    workflow_id="life_insurance_research",
)
```

Full campaign package example:

```python
create_life_insurance_campaign_package(
    user_name="Jean Pierre",
    client_name="MotherlyQuotes",
    target_audience="new and expecting moms",
    licensed_states=["CA"],
    product_focus="term life insurance",
    competitors=["Policygenius", "Ethos", "Ladder", "SelectQuote"],
    offer="free life insurance quote check",
    crm_destination="HubSpot",
    followup_channel="Brevo",
    timeout_seconds=300,
)
```

## Ping OS Supervisor

Ping OS is the supervisor/orchestrator layer for ChatGPT. Instead of calling workflow tools manually, ChatGPT can submit a business objective and let Ping OS choose the workflow graph.

Preferred stable interface:

```python
run_ping_os(
    objective="Create a full compliant campaign package to acquire qualified term life insurance leads from new and expecting moms in California.",
    business_name="MotherlyQuotes",
    vertical="life_insurance",
    target_audience="new and expecting moms",
    geography=["CA"],
    offer="free life insurance quote check",
    context={
        "product_focus": "term life insurance",
        "competitors": ["Policygenius", "Ethos", "Ladder", "SelectQuote"],
        "crm_destination": "HubSpot",
        "followup_channel": "Brevo",
        "licensed_states": ["CA"],
        "output_format": "markdown_and_json",
        "timeout_seconds": 300,
        "priority": "normal",
    },
)
```

Minimal call with MotherlyQuotes defaults:

```python
run_ping_os(
    objective="Research the California life insurance market for new parents.",
    business_name="MotherlyQuotes",
    vertical="life_insurance",
)
```

Debug through the same tool:

```python
run_ping_os(
    objective="debug",
    business_name="Ping OS",
    vertical="system",
    context={"action": "debug"},
)
```

Fetch a stored in-memory run through the same tool:

```python
run_ping_os(
    objective="get_run",
    business_name="Ping OS",
    vertical="system",
    context={"action": "get_run", "run_id": "ping-os-..."},
)
```

Supported verticals:

- `life_insurance`

Supported objective types:

- `lead_generation_campaign`
- `market_research`
- `content_engine`
- `voice_agent_setup`
- `compliance_review`
- `seo_strategy`
- `email_nurture`

The default life insurance lead-generation plan runs:

1. `life_insurance_research`
2. `life_insurance_seo`
3. `life_insurance_content`
4. `life_insurance_retell`
5. `life_insurance_email`
6. `life_insurance_compliance`

Legacy supervisor interface:

```python
run_ping_os_objective(
    objective="Generate a compliant campaign package to acquire 500 qualified life insurance leads in California this month.",
    business_name="MotherlyQuotes",
    vertical="life_insurance",
    target_audience="new and expecting moms",
    geography=["CA"],
    offer="free life insurance quote check",
    constraints={
        "product_focus": "term life insurance",
        "crm_destination": "HubSpot",
        "followup_channel": "Brevo",
        "competitors": ["Policygenius", "Ethos", "Ladder", "SelectQuote"],
    },
    output_format="markdown_and_json",
    timeout_seconds=300,
)
```

The response includes:

```json
{
  "ok": true,
  "run_id": "ping-os-...",
  "objective": "...",
  "business_name": "MotherlyQuotes",
  "vertical": "life_insurance",
  "execution_plan": [
    {
      "step": 1,
      "workflow_id": "life_insurance_research",
      "reason": "Research audience, competitors, buyer intent, objections, and campaign angles."
    }
  ],
  "workflow_results": {},
  "final_strategy": {},
  "markdown_report": ""
}
```

Fetch a stored run later:

```python
get_ping_os_run(run_id="ping-os-...")
```

Run records are stored in the MCP Gateway process memory and include:

- `run_id`
- `objective`
- `business_name`
- `vertical`
- `created_at`
- `status`
- `execution_plan`
- `workflow_ids`
- `kickoff_ids`
- `final_output`
- `markdown_report`

Persistent storage should be added later before relying on run retrieval across Render restarts, deploys, or multiple service instances.

## Connector Schema Notes

If ChatGPT only shows older tools such as `fetch_webpage`, `extract_links`, `check_url_status`, `analyze_text`, `run_crewai_automation`, and `call_crewai_endpoint`, the deployed server may still be correct. Verify server-side registration with local FastMCP introspection or by reconnecting the connector. The durable architecture is to expose and depend on one stable command tool, `run_ping_os`, then route future workflows internally.
