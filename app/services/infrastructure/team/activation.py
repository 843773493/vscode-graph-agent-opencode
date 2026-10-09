"""冻结 collaboration ledger 投影到独立 membership/task 语义 facet。"""

from __future__ import annotations

import json
from collections.abc import Mapping

from app.domain.itemized.hashing import canonical_json_bytes, sha256_jcs
from app.services.infrastructure.resource_platform.derivation.types import (
    ResourceSnapshot,
)

TEAM_FACETS = ("membership", "tasks")
TEAM_RESOURCE_IDS = {facet: f"team-state:{facet}:v1" for facet in TEAM_FACETS}
TEAM_SOURCE_IDS = {facet: f"team-source:{facet}:v1" for facet in TEAM_FACETS}
TEAM_LEDGER_SOURCE_ID = "collaboration-ledger:v1"


def team_guidance_content(payload: object) -> str:
    if not isinstance(payload, Mapping) or set(payload) != {"content"} or not isinstance(payload["content"], str):
        raise ValueError("team_state 冻结 payload 必须仅包含 content 文本")
    return payload["content"]


def team_projection(payload_json: str, thread_id: str) -> dict[str, dict[str, str]]:
    """只消费原 fanout 冻结值；不查当前成员或任务板。"""
    value = json.loads(payload_json)
    members = value["members"]
    visible_boards = []
    visible_members = []
    for board in value["boards"]:
        team_members = [member for member in members
                        if member["state"] != "registering" and member["child_thread_id"] is not None
                        and json.loads(member["task_seed"]).get("trusted_context", {}).get("team_id") == board["team_id"]]
        if thread_id != board["coordinator_thread_id"] and not any(
            member["child_thread_id"] == thread_id and member["state"] == "published" for member in team_members
        ):
            continue
        visible_boards.append(board)
        visible_members.append({
            "team_id": board["team_id"], "coordinator_thread_id": board["coordinator_thread_id"],
            "members": [{key: member[key] for key in ("delegation_id", "child_thread_id", "role", "state", "admission_state")}
                        for member in team_members],
        })
    return {
        facet: {"content": canonical_json_bytes({"session_id": value["session_id"], "thread_id": thread_id,
                                                 facet: selected}).decode() if selected else ""}
        for facet, selected in (("membership", visible_members), ("tasks", visible_boards))
    }


def team_resource_snapshots(*, payloads: Mapping[str, dict[str, str]], revision: int,
                            event_hash: str, generation: int, ready: bool) -> tuple[ResourceSnapshot, ...]:
    return tuple(ResourceSnapshot(
        resource_id=TEAM_RESOURCE_IDS[facet], resource_kind="team_state", facet=facet,
        display_uri=None, revision=sha256_jcs(payloads[facet]), payload=payloads[facet] if ready else None,
        source_lineage=((TEAM_LEDGER_SOURCE_ID, str(revision)), ("collaboration-event:v1", event_hash)),
        generation=generation, available=ready,
        error=None if ready else f"collaboration required revision={revision} 尚未完成 recipient 持久投递",
        error_code=None if ready else "source-unavailable",
        retained_revision=None if ready else sha256_jcs(payloads[facet]),
    ) for facet in TEAM_FACETS)
