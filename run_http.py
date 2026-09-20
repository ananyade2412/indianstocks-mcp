"""
run_http.py — run the MCP server over Streamable HTTP (for remote/claude.ai
custom-connector use), instead of stdio.

Env:  PORT (default 8000), HOST (default 127.0.0.1)
"""
import os
from server import mcp

if __name__ == "__main__":
    mcp.settings.host = os.environ.get("HOST", "127.0.0.1")
    mcp.settings.port = int(os.environ.get("PORT", "8000"))
    # FastMCP mounts the streamable-http endpoint at /mcp by default
    mcp.run(transport="streamable-http")
