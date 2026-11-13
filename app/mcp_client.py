import asyncio
import json
import os
import sys
from pathlib import Path

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client


class EdgarMCPClient:
    def __init__(self):
        project_root = Path(__file__).resolve().parent.parent
        environment = dict(os.environ)
        environment["PYTHONIOENCODING"] = "utf-8"
        self.server = StdioServerParameters(
            command=sys.executable,
            args=["-m", "app.mcp_server"],
            cwd=str(project_root),
            env=environment,
        )

    def call(self, tool_name: str, arguments: dict) -> dict:
        return asyncio.run(self._call(tool_name, arguments))

    async def _call(self, tool_name: str, arguments: dict) -> dict:
        async with stdio_client(self.server) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                result = await session.call_tool(tool_name, arguments)
                if result.isError:
                    text = "\n".join(getattr(item, "text", "") for item in result.content)
                    raise RuntimeError(f"MCP tool {tool_name} failed: {text}")
                structured = getattr(result, "structuredContent", None)
                if structured is not None:
                    return structured
                text = "".join(getattr(item, "text", "") for item in result.content)
                return json.loads(text)

    def list_tools(self) -> list[dict]:
        return asyncio.run(self._list_tools())

    async def _list_tools(self) -> list[dict]:
        async with stdio_client(self.server) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                result = await session.list_tools()
                return [{"name": tool.name, "description": tool.description} for tool in result.tools]
