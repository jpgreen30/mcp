from __future__ import annotations

import os

from crewai import Agent, Crew, Task
from crewai.mcp import MCPServerHTTP


MCP_URL = os.getenv("MCP_URL", "https://mcp-dh2a.onrender.com/mcp")
MCP_BEARER_TOKEN = os.environ["MCP_BEARER_TOKEN"]


research_tools = MCPServerHTTP(
    url=MCP_URL,
    headers={"Authorization": f"Bearer {MCP_BEARER_TOKEN}"},
    cache_tools_list=True,
)


researcher = Agent(
    role="Research Analyst",
    goal="Use remote MCP tools to inspect URLs and analyze text.",
    backstory=(
        "You are a practical research analyst with access to a remote "
        "Cloud Tools Gateway MCP server."
    ),
    mcps=[research_tools],
    verbose=True,
)


task = Task(
    description=(
        "Check the status of https://example.com, fetch the page text, "
        "and summarize the most important terms."
    ),
    expected_output="A short status report with URL health and top text terms.",
    agent=researcher,
)


crew = Crew(agents=[researcher], tasks=[task], verbose=True)


if __name__ == "__main__":
    result = crew.kickoff()
    print(result)
