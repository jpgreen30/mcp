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
