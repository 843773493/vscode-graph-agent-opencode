from __future__ import annotations

import json
from pathlib import Path

from tests.support.catalog_session_bundle import seed_catalog_session_bundle
from tests.support.session_catalog_owner import fixture_catalog_scope
from tests.support.workspaces import prepare_test_workspace

CANONICAL_SESSION_IDS = frozenset(
    {
        "ses_019c642bc2ec78718d0a296b3319d6df",
        "ses_019c27f816b17ee394a67f9312bb669e",
        "ses_019be55bdf8d715d8efbb8419fbc7cd7",
        "ses_019c3afc2c2674a58a36a8a7b1ad0f8b",
        "ses_019c46b09524769f8e70304bc6f23f31",
        "ses_019c29b9cf2e7c08858d95552eeb02e8",
    }
)


def prepare_custom_tool_test_workspace(
    *,
    workspace_root: Path,
    template_root: Path,
    shared_skill_root: Path | None = None,
) -> Path:
    """复制只读模板，并在测试副本重建完整 canonical rollout 历史。"""

    template_items = tuple(
        sorted(
            [item.name for item in template_root.iterdir() if item.name != ".boxteam"]
            + [".boxteam/AGENTS.md", ".boxteam/skills"]
        )
    )
    workspace_root = prepare_test_workspace(
        workspace_root=workspace_root,
        template_root=template_root,
        template_items=template_items,
        shared_skill_root=shared_skill_root,
    )
    manifest_path = workspace_root / "rollout-fixture.json"
    manifest_bytes = manifest_path.read_bytes()
    manifest = json.loads(manifest_bytes)
    if not isinstance(manifest, dict):
        raise TypeError(f"rollout fixture manifest 必须是 object: {manifest_path}")

    sessions = manifest.get("sessions")
    boundary_sessions = manifest.get("boundary_sessions")
    if not isinstance(sessions, list) or not isinstance(boundary_sessions, list):
        raise TypeError(f"rollout fixture manifest 缺少 sessions 清单: {manifest_path}")
    manifest_session_ids = {
        item["session_id"]
        for item in sessions
        if isinstance(item, dict) and isinstance(item.get("session_id"), str)
    }
    if not CANONICAL_SESSION_IDS.issubset(manifest_session_ids):
        raise ValueError(
            "rollout fixture manifest 与 canonical 生成器的固定 session 清单不一致: "
            f"missing={sorted(CANONICAL_SESSION_IDS - manifest_session_ids)}"
        )

    from scripts.generate_custom_tool_rollout_fixture import (
        generate_custom_tool_rollout_fixture,
    )

    generate_custom_tool_rollout_fixture(workspace_root)
    manifest_path.write_bytes(manifest_bytes)

    with fixture_catalog_scope():
        sessions_root = workspace_root / ".boxteam" / "sessions"
        boundary_session_ids: list[str] = []
        for item in boundary_sessions:
            if not isinstance(item, dict):
                raise TypeError(f"rollout fixture boundary entry 必须是 object: {item!r}")
            session_id = item.get("session_id")
            title = item.get("title")
            if not isinstance(session_id, str) or not isinstance(title, str):
                raise TypeError(f"rollout fixture boundary entry 缺少 identity/title: {item!r}")
            seed_catalog_session_bundle(sessions_root, session_id, title=title)
            boundary_session_ids.append(session_id)

        from tests.support.web_boundary_seeding import seed_boundary_cases

        seed_boundary_cases(workspace_root, session_ids=boundary_session_ids)
        return workspace_root
