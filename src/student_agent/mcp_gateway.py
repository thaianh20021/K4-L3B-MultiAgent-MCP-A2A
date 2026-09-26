from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from copy import deepcopy
from typing import Any

import httpx2
from jsonschema import Draft202012Validator
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.types import PaginatedRequestParams

from .contracts import Contracts


class EvidenceGateway:
    def __init__(self, session: ClientSession, contracts: Contracts) -> None:
        self._session = session
        self._contracts = contracts
        self._tools: dict[str, dict[str, Any]] | None = None
        self._owners: dict[str, str] = {}

    async def discover_tools(self) -> dict[str, dict[str, Any]]:
        if self._tools is None:
            tools = {}
            cursor = None
            while True:
                params = PaginatedRequestParams(cursor=cursor) if cursor else None
                response = await self._session.list_tools(params=params)
                for tool in response.tools:
                    schema = getattr(tool, "input_schema", None)
                    if schema is None:
                        schema = getattr(tool, "inputSchema", {})
                    tools[tool.name] = schema
                cursor = getattr(response, "next_cursor", None) or getattr(
                    response, "nextCursor", None
                )
                if not cursor:
                    break
            self._tools = tools
        return deepcopy(self._tools)

    async def list_tools(self) -> list[str]:
        return sorted(await self.discover_tools())

    async def call(self, tool_name: str, *, case_id: str, **arguments: Any) -> dict[str, Any]:
        tools = await self.discover_tools()
        if tool_name not in tools:
            raise ValueError(f"MCP tool was not discovered: {tool_name}")
        payload = {"case_id": case_id, **arguments}
        Draft202012Validator(tools[tool_name]).validate(payload)
        result = await self._session.call_tool(tool_name, arguments=payload)
        if getattr(result, "is_error", getattr(result, "isError", False)):
            message = " ".join(
                block.text for block in result.content if getattr(block, "text", None)
            )
            raise RuntimeError(f"MCP tool {tool_name} failed: {message or 'unknown error'}")
        evidence = getattr(result, "structuredContent", None)
        if evidence is None:
            evidence = getattr(result, "structured_content", None)
        if evidence is None:
            text_blocks = [block.text for block in result.content if getattr(block, "text", None)]
            if len(text_blocks) != 1:
                raise ValueError(f"MCP tool {tool_name} did not return one evidence object")
            evidence = json.loads(text_blocks[0])
        self._contracts.validate_evidence(evidence, f"MCP tool {tool_name}")
        owner = self._owners.setdefault(evidence["evidence_ref"], case_id)
        if owner != case_id:
            raise ValueError("MCP returned an evidence_ref already owned by another case")
        return evidence


@asynccontextmanager
async def connect_gateway(
    endpoint: str, team_api_key: str, contracts: Contracts
) -> AsyncIterator[EvidenceGateway]:
    headers = {"Authorization": f"Bearer {team_api_key}"}
    timeout = httpx2.Timeout(300.0, connect=30.0, write=30.0, pool=30.0)
    async with (
        httpx2.AsyncClient(headers=headers, timeout=timeout) as http_client,
        streamable_http_client(endpoint, http_client=http_client) as (read_stream, write_stream),
        ClientSession(read_stream, write_stream) as session,
    ):
        await session.initialize()
        yield EvidenceGateway(session, contracts)
