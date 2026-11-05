"""候选只读取所选目录的原始条目与本目标平台，不保存计划或运行 MCP。"""

from __future__ import annotations

import json
import platform
from urllib.parse import unquote, urlsplit

import httpx

from app.domain.mcp_candidates import McpCandidateConflict
from app.schemas.internal_v2.mcp import McpCandidateSourceDTO, McpTargetPlatformDTO

_MAX_MANIFEST_BYTES = 5 * 1024 * 1024
_SOURCE_TIMEOUT_SECONDS = 30


class McpCandidateSourceUnavailable(RuntimeError):
    """目录读取失败不等于无匹配，也不能改投另一来源。"""


def native_mcp_platform() -> McpTargetPlatformDTO:
    return McpTargetPlatformDTO(operating_system=platform.system().lower(), architecture=platform.machine().lower())


def _gallery_entry_url(source: McpCandidateSourceDTO) -> str:
    assert source.endpoint is not None and source.entry_url is not None and source.protocol is not None
    endpoint, entry = urlsplit(source.endpoint), urlsplit(source.entry_url)
    for url in (endpoint, entry):
        if url.scheme not in {"http", "https"} or not url.hostname or url.username or url.password or url.fragment:
            raise ValueError("MCP 目录地址必须是无凭据及 fragment 的 HTTP(S) URL")
        decoded = unquote(url.path)
        while unquote(decoded) != decoded:
            decoded = unquote(decoded)
        if "\\" in decoded or any(part in {".", ".."} for part in decoded.split("/")):
            raise ValueError("MCP 目录地址不能包含路径跳转")
    scope = endpoint.path.rstrip("/") + f"/{source.protocol}/servers/"
    if (endpoint.scheme, endpoint.netloc.lower()) != (entry.scheme, entry.netloc.lower()) or not entry.path.startswith(scope):
        raise ValueError("MCP entry_url 不属于显式选择的目录 endpoint 和协议")
    if endpoint.query:
        raise ValueError("MCP 目录 endpoint 不能携带查询参数")
    return source.entry_url


async def read_candidate_source(source: McpCandidateSourceDTO) -> object:
    if source.kind == "manual":
        return {"name": source.name, "version": source.version, "definition": source.definition}
    url = _gallery_entry_url(source)
    try:
        async with (
            httpx.AsyncClient(timeout=_SOURCE_TIMEOUT_SECONDS, trust_env=False, follow_redirects=False) as client,
            client.stream("GET", url) as response,
        ):
            if response.status_code == 404:
                raise McpCandidateConflict("所选 MCP 来源条目已消失，请重新选择")
            if response.status_code != 200:
                raise McpCandidateSourceUnavailable(f"MCP 目录条目读取失败: HTTP {response.status_code}")
            chunks: list[bytes] = []
            size = 0
            async for chunk in response.aiter_bytes():
                size += len(chunk)
                if size > _MAX_MANIFEST_BYTES:
                    raise McpCandidateSourceUnavailable("MCP 目录 manifest 超出有界读取上限")
                chunks.append(chunk)
    except httpx.RequestError as error:
        raise McpCandidateSourceUnavailable("MCP 目录条目请求失败，未尝试其它来源或协议") from error
    try:
        return json.loads(b"".join(chunks))
    except (ValueError, UnicodeError) as error:
        raise McpCandidateSourceUnavailable("MCP 目录返回无效 JSON manifest") from error
