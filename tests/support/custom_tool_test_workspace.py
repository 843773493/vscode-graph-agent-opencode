from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from time import perf_counter_ns
from uuid import uuid4

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


class FixtureStageObserver:
    """仅供显式 profile 使用的标量 JSONL 计时记录器。"""

    def __init__(self, artifacts_root: Path) -> None:
        artifacts_root.mkdir(parents=True, exist_ok=True)
        self.path = artifacts_root / f"fixture-stage-timing-{uuid4().hex}.jsonl"
        self._stream = self.path.open("x", encoding="utf-8")
        self._sequence = 0
        self._normal_prepare_calls = 0
        self._completed_normal_turns = 0
        self._completed_fork_turns = 0
        self._closed = False
        self.record(
            "start",
            "fixture.profile",
            expected_total_turns=427,
            expected_normal_turns=420,
            expected_fork_turns=7,
        )

    def record(self, event: str, stage: str, **fields: object) -> None:
        if any(not isinstance(value, (str, int, float, bool, type(None))) for value in fields.values()):
            raise TypeError("fixture profile 只允许记录标量字段")
        self._sequence += 1
        self._stream.write(
            json.dumps(
                {
                    "sequence": self._sequence,
                    "event": event,
                    "stage": stage,
                    "monotonic_ns": perf_counter_ns(),
                    **fields,
                },
                ensure_ascii=False,
                separators=(",", ":"),
            )
            + "\n"
        )
        self._stream.flush()
        if event == "start" and stage == "generator.prepare_execution":
            self._normal_prepare_calls += 1
        elif event == "done" and fields.get("outcome") == "ok":
            if stage == "generator.normal_turn":
                self._completed_normal_turns += 1
            elif stage == "generator.fork_copy":
                fork_turns = fields.get("turn_count")
                if isinstance(fork_turns, int):
                    self._completed_fork_turns += fork_turns

    def close(self) -> None:
        if self._closed:
            return
        self.record(
            "summary",
            "fixture.profile",
            total_turns=self._completed_normal_turns + self._completed_fork_turns,
            normal_turns=self._completed_normal_turns,
            normal_prepare_calls=self._normal_prepare_calls,
            fork_turns=self._completed_fork_turns,
        )
        self._closed = True
        self._stream.flush()
        self._stream.close()


@contextmanager
def timed_fixture_stage(
    observer: FixtureStageObserver | None,
    stage: str,
    **fields: object,
) -> Iterator[dict[str, object] | None]:
    if observer is None:
        yield None
        return

    started_ns = perf_counter_ns()
    observer.record("start", stage, inclusive=True, **fields)
    result_fields: dict[str, object] = {}
    try:
        yield result_fields
    except BaseException as error:
        observer.record(
            "done",
            stage,
            duration_ns=perf_counter_ns() - started_ns,
            inclusive=True,
            outcome="error",
            error_type=type(error).__name__,
            **fields,
            **result_fields,
        )
        raise
    else:
        observer.record(
            "done",
            stage,
            duration_ns=perf_counter_ns() - started_ns,
            inclusive=True,
            outcome="ok",
            **fields,
            **result_fields,
        )


def prepare_custom_tool_test_workspace(
    *,
    workspace_root: Path,
    template_root: Path,
    shared_skill_root: Path | None = None,
    observer: FixtureStageObserver | None = None,
) -> Path:
    """复制只读模板，并在测试副本重建完整 canonical rollout 历史。"""

    template_items = tuple(
        sorted(
            [item.name for item in template_root.iterdir() if item.name != ".boxteam"]
            + [".boxteam/AGENTS.md", ".boxteam/skills"]
        )
    )
    with timed_fixture_stage(observer, "workspace.template_copy"):
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

    with timed_fixture_stage(observer, "workspace.generator", total_turns=427, normal_turns=420, fork_turns=7):
        generate_custom_tool_rollout_fixture(workspace_root, observer=observer)
    with timed_fixture_stage(observer, "workspace.manifest_restore"):
        manifest_path.write_bytes(manifest_bytes)

    with fixture_catalog_scope():
        sessions_root = workspace_root / ".boxteam" / "sessions"
        boundary_session_ids: list[str] = []
        with timed_fixture_stage(observer, "workspace.boundary_catalog_seed", count=len(boundary_sessions)):
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

        with timed_fixture_stage(observer, "workspace.boundary_web_seed", count=len(boundary_session_ids)):
            seed_boundary_cases(workspace_root, session_ids=boundary_session_ids)
        return workspace_root
