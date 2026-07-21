from __future__ import annotations

from mcp.server.fastmcp import FastMCP


server = FastMCP("BoxTeam E2E Mini MCP")
counter = 0


@server.tool()
def echo(text: str) -> str:
    """返回带固定前缀的输入文本。"""
    return f"mini-mcp:{text}"


@server.tool()
def increment() -> int:
    """递增并返回进程内计数器，用于验证 MCP session 生命周期。"""
    global counter
    counter += 1
    return counter


if __name__ == "__main__":
    server.run(transport="stdio")
