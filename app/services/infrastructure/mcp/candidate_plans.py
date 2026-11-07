"""安装与配置候选共用的解析链；工作区私有输入仅在明确配置上下文消费。"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import cast

from app.domain.itemized.hashing import sha256_jcs
from app.domain.mcp_candidates import (
    McpCandidateConflict,
    McpCandidateManifest,
    candidate_parameters,
    parse_candidate_manifest,
)
from app.schemas.internal_v2.mcp import (
    McpCandidateInspectDTO,
    McpCandidateInspectRequestDTO,
    McpCandidatePlanDTO,
    McpCandidatePlanRequestDTO,
    McpCandidateProvenanceDTO,
    McpInstallationTargetDTO,
    McpInstalledArtifactDTO,
    McpManagedLaunchDTO,
    McpPrivateEnvRefDTO,
)
from app.services.infrastructure.config.source_document import redact_source_payload
from app.services.infrastructure.mcp.candidate_source import (
    native_mcp_platform,
    read_candidate_source,
)
from app.services.infrastructure.mcp.config import parse_mcp_server_configs
from app.services.infrastructure.mcp.private_inputs import (
    McpPrivateInputStore,
    candidate_env_definition,
)


async def read_installation_candidate(
    request: McpCandidateInspectRequestDTO, actual_target: McpInstallationTargetDTO,
) -> tuple[McpCandidateManifest, McpCandidateProvenanceDTO]:
    if request.target != actual_target:
        raise McpCandidateConflict("MCP 安装目标不属于当前原生 Gateway／Backend，不能改投当前工作区")
    source = request.source
    raw = await read_candidate_source(source)
    manifest = parse_candidate_manifest(raw, kind=source.kind, protocol=source.protocol,
        name=source.name, version=source.version, entry_id=source.entry_id)
    provenance = McpCandidateProvenanceDTO(
        kind=source.kind, name=source.name, version=source.version,
        endpoint=source.endpoint.rstrip("/") if source.endpoint is not None else None,
        protocol=source.protocol, entry_url=source.entry_url, entry_id=manifest.entry_id,
    )
    return manifest, provenance


async def inspect_candidate(
    request: McpCandidateInspectRequestDTO, *, actual_target: McpInstallationTargetDTO,
) -> McpCandidateInspectDTO:
    """只列出原件中的全部 variant；选择、安装、配置和连接分别发生。"""
    manifest, provenance = await read_installation_candidate(request, actual_target)
    return McpCandidateInspectDTO.model_validate({
        "source": provenance, "manifest_digest": manifest.digest, "platform": native_mcp_platform(),
        "variants": [variant.describe() for variant in manifest.variants],
    })


async def resolve_candidate(
    request: McpCandidatePlanRequestDTO, *, actual_target: McpInstallationTargetDTO,
    workspace_root: Path | None = None,
    installation_resolver: Callable[[McpManagedLaunchDTO], McpInstalledArtifactDTO] | None = None,
) -> tuple[McpCandidatePlanDTO, dict[str, object]]:
    manifest, provenance = await read_installation_candidate(request, actual_target)
    selected = request.variant
    variant = manifest.select(request.manifest_digest, selected.kind, selected.index, selected.digest)
    disposition, parameters = candidate_parameters(variant, request.inputs,
        purpose="configuration" if workspace_root is not None else "installation")
    server_id = request.server_id or "candidate"
    if disposition == "requires_artifact":
        definition = candidate_env_definition(provenance, manifest.digest, selected)
        for key, value in cast(dict[str, object], parameters.get("env", {})).items():
            if not isinstance(value, str):
                if request.server_id is None:
                    raise ValueError("MCP 私有 env 候选必须指定精确 server_id")
                if workspace_root is None:
                    raise ValueError("MCP 私有输入仅可在明确工作区的配置计划中消费")
                McpPrivateInputStore(workspace_root=workspace_root).require_env_binding(McpPrivateEnvRefDTO.model_validate(value), server_id=server_id,
                    input_key=key, definition_digest=definition)
    normalized = parameters
    if disposition != "requires_artifact":
        server, = parse_mcp_server_configs({"servers": {server_id: parameters}}, workspace_root=workspace_root,
            installation_resolver=installation_resolver)
        # 沿原配置 parser 在目标侧解析环境引用，不向浏览器泄露 env/headers 值。
        assert server.connection is not None
        normalized = dict(server.connection)
        if server.managed_launch is not None:
            normalized["managed_launch"] = server.managed_launch.model_dump(mode="json")
            normalized.pop("command")
        if server.private_env:
            normalized["env"] = {**cast(dict[str, object], normalized.get("env", {})),
                **{reference.input_key: reference.model_dump(mode="json") for reference in server.private_env}}
        if server.private_headers:
            normalized["headers"] = {**cast(dict[str, object], normalized.get("headers", {})),
                **{reference.header_name: reference.model_dump(mode="json") for reference in server.private_headers}}
        if server.oauth is not None:
            normalized["oauth"] = {"redirect_uri": server.oauth.redirect_uri}
    platform = native_mcp_platform()
    fingerprint = {"target": actual_target.model_dump(), "source": provenance.model_dump(),
                   "manifest_digest": manifest.digest, "variant": variant.ref(), "platform": platform.model_dump(),
                   "disposition": disposition, "parameters": normalized}
    if request.server_id is not None:
        fingerprint["server_id"] = request.server_id
    redacted = redact_source_payload(normalized, ("mcp", "servers", "candidate"))
    return McpCandidatePlanDTO.model_validate({
        "source": provenance, "manifest_digest": manifest.digest, "variant": variant.ref(), "platform": platform,
        "plan_digest": sha256_jcs(fingerprint), "disposition": disposition,
        "download_required": disposition == "requires_artifact", "parameters": redacted,
    }), parameters
