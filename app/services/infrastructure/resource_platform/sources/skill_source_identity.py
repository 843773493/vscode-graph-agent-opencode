"""来源身份和认证快照的唯一协议编码；获取地址不参与模型可见引用。"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from typing import Literal

from app.core.resource_uri import skill_display_uri
from app.domain.itemized.hashing import canonical_json_bytes
from app.schemas.gateway.skill_sources import (
    GatewaySkillSourceEntryDTO,
    PluginSkillSourceDTO,
)


def plugin_skill_identity(gateway_id: str, source: PluginSkillSourceDTO) -> tuple[str, str, str]:
    """沿原 skills URI 注册确切包来源，不把安装路径或实例代际写入引用。"""
    identity = hashlib.sha256(canonical_json_bytes([
        gateway_id, source.workspace_id, source.package_id, source.plugin_id,
        source.package_version, source.package_digest, source.contribution_id,
    ])).hexdigest()
    return (
        # 原 source_lineage 会封存此身份；卸载包后仍能核对原目标、包版本及贡献。
        ":".join(("gateway-plugin-skill-source", gateway_id, source.workspace_id or "gateway",
                  source.package_id, source.plugin_id, source.package_version,
                  source.package_digest, source.contribution_id)),
        f"skill-entry:plugin:{identity}",
        skill_display_uri(scope="gateway", scope_id=gateway_id, skill_name=f"plugin_{identity}"),
    )


def configured_skill_identity(scope: Literal["gateway", "workspace"], scope_id: str, source_id: str) -> tuple[str, str, str]:
    # v1 身份字节固定；http 是既有身份命名，不决定获取协议或触发方式。
    # 下划线不属于合法 Skill name，避免与本地同名 Skill 的资源根混用。
    return (
        f"skill-source:{scope}:{scope_id}:http:{source_id}",
        f"skill-entry:{scope}:{scope_id}:http:{source_id}",
        skill_display_uri(scope=scope, scope_id=scope_id, skill_name=f"http_{source_id}"),
    )


def gateway_skill_revision(gateway_id: str, available: bool, entries: Sequence[GatewaySkillSourceEntryDTO]) -> str:
    def identity(entry: GatewaySkillSourceEntryDTO) -> tuple[object, ...]:
        fields = (entry.source_id, entry.revision, entry.available, entry.name,
                  entry.layer, entry.priority, entry.display_uri)
        # 非插件来源保持既有 revision 编码；包来源额外冻结贡献与工具关联。
        return (*fields, entry.plugin.model_dump(mode="json")) if entry.plugin is not None else fields

    return "sha256:" + hashlib.sha256(canonical_json_bytes({
        "gateway_id": gateway_id, "available": available,
        "entries": [identity(entry) for entry in entries],
    })).hexdigest()
