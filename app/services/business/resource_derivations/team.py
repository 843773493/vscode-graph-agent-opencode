"""从已接受的 collaboration 冻结值派生独立的团队语义 facet。"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import ClassVar

from app.domain.itemized.hashing import canonical_json_bytes
from app.services.infrastructure.resource_platform.derivation.types import (
    SemanticInput,
    SemanticPayload,
)

TEAM_FACETS = ("membership", "tasks")
TEAM_RESOURCE_IDS = {facet: f"team-state:{facet}:v1" for facet in TEAM_FACETS}
TEAM_SOURCE_IDS = {facet: f"team-source:{facet}:v1" for facet in TEAM_FACETS}
TEAM_LEDGER_SOURCE_ID = "collaboration-ledger:v1"
TEAM_EVENT_SOURCE_ID = "collaboration-event:v1"


def team_guidance_content(payload: object) -> str:
    if not isinstance(payload, Mapping) or set(payload) != {"content"} or not isinstance(payload["content"], str):
        raise ValueError("team_state 冻结 payload 必须仅包含 content 文本")
    return payload["content"]


def team_facet_payload(payload_json: str, thread_id: str, facet: str) -> dict[str, str]:
    """唯一投影同时供 loader 与 Saver 校验原持久 observation 使用。"""
    if facet not in TEAM_FACETS:
        raise ValueError(f"未知团队 facet: {facet}")
    value = json.loads(payload_json)
    selected = []
    for board in value["boards"]:
        team_members = [member for member in value["members"]
                        if member["state"] != "registering" and member["child_thread_id"] is not None
                        and json.loads(member["task_seed"]).get("trusted_context", {}).get("team_id") == board["team_id"]]
        if thread_id != board["coordinator_thread_id"] and not any(
            member["child_thread_id"] == thread_id and member["state"] == "published" for member in team_members
        ):
            continue
        selected.append(board if facet == "tasks" else {
            "team_id": board["team_id"], "coordinator_thread_id": board["coordinator_thread_id"],
            "members": [{key: member[key] for key in ("delegation_id", "child_thread_id", "role", "state", "admission_state")}
                        for member in team_members],
        })
    return {"content": canonical_json_bytes({"session_id": value["session_id"], "thread_id": thread_id,
                                             facet: selected}).decode() if selected else ""}


@dataclass(frozen=True, slots=True)
class TeamFacetLoader:
    """只接受同一原 fanout 的 ledger/event 向量，不访问当前团队状态。"""

    version: ClassVar[str] = "team-ledger-facet:v1"
    session_id: str
    thread_id: str
    facet: str

    def __post_init__(self) -> None:
        if self.facet not in TEAM_FACETS:
            raise ValueError(f"未知团队 facet: {self.facet}")

    def __call__(self, inputs: Mapping[str, SemanticInput]) -> SemanticPayload:
        ledger = inputs[TEAM_LEDGER_SOURCE_ID]
        event = inputs[TEAM_EVENT_SOURCE_ID]
        if not isinstance(ledger.content, str):
            return SemanticPayload(available=False, error="Team 已接受 ledger 正文必须是文本")
        try:
            value = json.loads(ledger.content)
        except json.JSONDecodeError as error:
            return SemanticPayload(available=False, error=f"Team 冻结 ledger JSON 无效: {error.msg}")
        if not isinstance(value, dict) or not isinstance(value.get("boards"), list) or not isinstance(value.get("members"), list):
            return SemanticPayload(available=False, error="Team 冻结 ledger 缺少 boards/members")
        if (
            value.get("session_id") != self.session_id
            or str(value.get("ledger_revision")) != ledger.revision
            or value.get("event_hash") != event.revision
            or event.content != event.revision
        ):
            return SemanticPayload(available=False, error="Team 已接受来源 owner/revision/event 向量不一致")
        return SemanticPayload(payload=team_facet_payload(ledger.content, self.thread_id, self.facet))
