"""来源身份和认证快照的唯一协议编码；获取地址不参与模型可见引用。"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from typing import Literal

from app.core.resource_uri import skill_display_uri
from app.domain.itemized.hashing import canonical_json_bytes
from app.schemas.gateway.skill_sources import GatewaySkillSourceEntryDTO


def http_skill_identity(scope: Literal["gateway", "workspace"], scope_id: str, source_id: str) -> tuple[str, str, str]:
    # 下划线不属于合法 Skill name，避免与本地同名 Skill 的资源根混用。
    return (
        f"skill-source:{scope}:{scope_id}:http:{source_id}",
        f"skill-entry:{scope}:{scope_id}:http:{source_id}",
        skill_display_uri(scope=scope, scope_id=scope_id, skill_name=f"http_{source_id}"),
    )


def gateway_skill_revision(gateway_id: str, available: bool, entries: Sequence[GatewaySkillSourceEntryDTO]) -> str:
    return "sha256:" + hashlib.sha256(canonical_json_bytes({
        "gateway_id": gateway_id, "available": available,
        "entries": [(entry.source_id, entry.revision, entry.available, entry.name,
                     entry.layer, entry.priority, entry.display_uri) for entry in entries],
    })).hexdigest()
