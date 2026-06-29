"""
MCP (Model Context Protocol) Client implementation.

Provides support for connecting to MCP servers via stdio or SSE,
discovering tools, and wrapping them for the ToolRegistry.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shlex
import aiohttp

from typing import Any
from config.settings import MCPServerConfig
from core.tools import BaseTool, ToolRegistry

logger = logging.getLogger(__name__)


class MCPClient:
    """Manages connection to a single MCP server."""

    def __init__(self, config: MCPServerConfig):
        self.config = config
        self._process: asyncio.subprocess.Process | None = None
        self._sse_session: aiohttp.ClientSession | None = None
        self._msg_id = 1
        self._pending_requests: dict[int, asyncio.Future] = {}
        self._listen_task: asyncio.Task | None = None

    async def connect(self) -> None:
        """Establish connection."""
        if self.config.transport == "stdio":
            await self._connect_stdio()
        elif self.config.transport == "sse":
            await self._connect_sse()
        else:
            raise ValueError(f"Unknown transport: {self.config.transport}")

    async def _connect_stdio(self) -> None:
        import os
        env = os.environ.copy()
        if self.config.env:
            env.update(self.config.env)

        cmd = [self.config.command] + self.config.args
        cmd_str = " ".join(shlex.quote(arg) for arg in cmd)
        
        logger.info("Starting MCP stdio server: %s", cmd_str)
        self._process = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        self._listen_task = asyncio.create_task(self._listen_stdio())

    async def _connect_sse(self) -> None:
        # ponytail: SSE not implemented. To add it, read the SSE stream for
        # responses and POST requests to the endpoint; until then use stdio.
        raise NotImplementedError(
            "MCP 'sse' transport is not implemented. Use transport='stdio', "
            "or implement proper SSE in core/mcp.py._connect_sse."
        )

    async def disconnect(self) -> None:
        """Clean up connection."""
        if self._listen_task:
            self._listen_task.cancel()
            try:
                await self._listen_task
            except asyncio.CancelledError:
                pass
            self._listen_task = None

        if self._process:
            if self._process.returncode is None:
                self._process.terminate()
                try:
                    await asyncio.wait_for(self._process.wait(), timeout=5.0)
                except asyncio.TimeoutError:
                    self._process.kill()
            self._process = None

        if self._sse_session:
            await self._sse_session.close()
            self._sse_session = None

        for fut in self._pending_requests.values():
            if not fut.done():
                fut.set_exception(ConnectionError("MCP Client disconnected"))
        self._pending_requests.clear()

    async def _listen_stdio(self) -> None:
        if not self._process or not self._process.stdout:
            return
            
        try:
            while True:
                line = await self._process.stdout.readline()
                if not line:
                    break
                try:
                    msg = json.loads(line.decode("utf-8"))
                    self._handle_message(msg)
                except json.JSONDecodeError:
                    logger.debug("Failed to decode MCP message: %s", line)
        except Exception as e:
            logger.error("Error in MCP stdio listener: %s", e)

    def _handle_message(self, msg: dict[str, Any]) -> None:
        msg_id = msg.get("id")
        if msg_id is not None and msg_id in self._pending_requests:
            fut = self._pending_requests.pop(msg_id)
            if not fut.done():
                if "error" in msg:
                    fut.set_exception(RuntimeError(f"MCP RPC Error: {msg['error']}"))
                else:
                    fut.set_result(msg.get("result"))

    async def _send_request(self, method: str, params: dict[str, Any] | None = None) -> Any:
        req_id = self._msg_id
        self._msg_id += 1
        
        req = {
            "jsonrpc": "2.0",
            "id": req_id,
            "method": method,
            "params": params or {}
        }
        
        fut = asyncio.get_running_loop().create_future()
        self._pending_requests[req_id] = fut
        
        if not self._process or not self._process.stdin:
            raise ConnectionError("Stdio process not running")
        req_bytes = json.dumps(req).encode("utf-8") + b"\n"
        self._process.stdin.write(req_bytes)
        await self._process.stdin.drain()

        return await fut

    async def list_tools(self) -> list[dict]:
        """Discover available tools from the MCP server."""
        try:
            result = await self._send_request("tools/list")
            return result.get("tools", [])
        except Exception as e:
            logger.error("Failed to list tools for MCP server %s: %s", self.config.name, e)
            return []

    async def call_tool(self, name: str, arguments: dict) -> str:
        """Execute a tool on the MCP server."""
        try:
            result = await self._send_request("tools/call", {"name": name, "arguments": arguments})
            content = result.get("content", [])
            text_parts = []
            for part in content:
                if part.get("type") == "text":
                    text_parts.append(part.get("text", ""))
            return "\n".join(text_parts)
        except Exception as e:
            return f"Error executing tool {name} via MCP: {e}"


class MCPToolWrapper(BaseTool):
    """Wraps an MCP server tool as a BaseTool for the ToolRegistry."""
    
    def __init__(self, client: MCPClient, tool_schema: dict):
        self._client = client
        self.name = tool_schema.get("name", "unknown_mcp_tool")
        self.description = tool_schema.get("description", "")
        self.parameters = tool_schema.get("inputSchema", {"type": "object", "properties": {}})
        self.destructive = False # By default we assume MCP tools are non-destructive or handle their own auth
        
    async def execute(self, **params) -> str:
        return await self._client.call_tool(self.name, params)


class MCPManager:
    """Manages all configured MCP servers and their tools."""
    
    def __init__(self):
        self.clients: list[MCPClient] = []
        
    async def initialize(self, servers: list[MCPServerConfig], registry: ToolRegistry) -> None:
        """Connect to all servers, discover tools, register them."""
        for server_config in servers:
            client = MCPClient(server_config)
            try:
                await client.connect()
                self.clients.append(client)
                tools = await client.list_tools()
                for tool in tools:
                    wrapper = MCPToolWrapper(client, tool)
                    registry.register(wrapper)
                    logger.info("Registered MCP tool: %s", wrapper.name)
            except Exception as e:
                logger.error("Failed to initialize MCP server %s: %s", server_config.name, e)
                
    async def shutdown(self) -> None:
        """Disconnect all servers."""
        for client in self.clients:
            await client.disconnect()
        self.clients.clear()
