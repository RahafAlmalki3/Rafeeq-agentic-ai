"""Madar's read tools, policies and triage prompt as an MCP server, bound to one Madar database.

`build(m)` takes an existing `madar.Madar`, so the agent's MCP tools and its local write tools read and write the same
database (an inspection booked locally is visible to `get_permit` over MCP). The agent connects in-process; run it
standalone to show it to any MCP host:

    uv run python -m rafeeq.mcp_server                 # stdio, its own database copy
    uv run python -m rafeeq.mcp_server --http 8765     # Streamable HTTP on http://127.0.0.1:8765/mcp
"""

from __future__ import annotations

import argparse
import functools
import inspect

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ResourceNotFoundError, ToolError

from .kit import madar


def anticipated(fn):
    """Madar's ToolError → MCP's ToolError, so the model reads the message (other exceptions stay hidden)."""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except madar.ToolError as e:
            raise ToolError(str(e)) from e

    return wrapper


def build(m: madar.Madar) -> MCPServer:
    """The server for one Madar database. Read tools only: writes stay in the agent's harness, behind approvals."""
    server = MCPServer(
        "madar",
        title="Madar Municipal Services",
        instructions="Read tools and policies of Madar Municipal Services (a fictional municipality). "
                     "Verify identity with get_permit before disclosing anything about an application.",
        version="1.1.0",
        log_level="WARNING",
    )
    for name in m.READ_TOOLS:
        fn = getattr(m, name)
        server.tool(name=name, description=inspect.getdoc(fn))(anticipated(fn))

    @server.resource("madar://policies", mime_type="text/markdown")
    def policy_index() -> str:
        """The list of Madar policies with their IDs and titles."""
        return "\n".join(f"- {p['id']}: {p['title']} ({p['title_ar']})" for p in madar.load_policies())

    @server.resource("policy://{policy_id}", mime_type="text/markdown")
    def policy(policy_id: str) -> str:
        """One Madar policy document, e.g. policy://POL-02."""
        for p in madar.load_policies():
            if p["id"].lower() == policy_id.lower():
                return p["text"]
        raise ResourceNotFoundError(f"No policy {policy_id!r}. Read madar://policies for the list.")

    @server.prompt()
    def triage_email(subject: str, body: str) -> str:
        """Classify one email sent to Madar (label, permit ID, language, needs_human)."""
        return (
            "You triage messages sent to Madar Municipal Services. Reply with JSON only: "
            '{"label": one of status|escalation|inspection|appeal|needs_info|refund|fees|rules|other, '
            '"permit_id": string or null, "lang": "ar" or "en", "needs_human": boolean}.\n\n'
            f"Subject: {subject}\n\n{body}"
        )

    return server


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--http", type=int, metavar="PORT", help="serve Streamable HTTP on this port instead of stdio")
    ap.add_argument("--workdir", default=str(madar.ROOT / "artifacts" / "mcp_server"), help="folder for the database copy")
    a = ap.parse_args()
    srv = build(madar.Madar(a.workdir))
    if a.http:
        srv.run(transport="streamable-http", host="127.0.0.1", port=a.http)
    else:
        srv.run()
