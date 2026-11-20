"""rollout SQLite 27→28：用原冻结正文生成团队投递 facet 回执。"""

from __future__ import annotations

import json
import sqlite3

from app.core.session_control_store.collaboration import task_team_id
from app.domain.itemized.hashing import canonical_json_bytes
from app.services.infrastructure.rollout_context.checkpoint.durable.source_activation import (
    _team_observation_hashes,
)


def migrate_team_source_observations(connection: sqlite3.Connection) -> None:
    """显式27→28离线迁移；正文只用于计算原冻结 facet，不进入新运行时。"""
    for session_id, thread_id, revision, event_hash, raw in connection.execute(
        "SELECT session_id,thread_id,ledger_revision,event_hash,facet_hashes_json "
        "FROM team_source_observations"
    ):
        payload = json.loads(raw)
        for member in payload["members"]:
            member["team_id"] = task_team_id(member.pop("task_seed"))
        hashes = _team_observation_hashes(
            session_id, thread_id, revision, event_hash, canonical_json_bytes(payload).decode()
        )
        connection.execute(
            "UPDATE team_source_observations SET facet_hashes_json=? "
            "WHERE session_id=? AND thread_id=? AND ledger_revision=?",
            (hashes, session_id, thread_id, revision),
        )
