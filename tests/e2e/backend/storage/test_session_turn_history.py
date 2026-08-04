from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest

from app.core.path_utils import get_session_path_resolver
from app.schemas.event import Event, JobCompletedEvent, JobCompletedPayload
from app.services.business.session_turn_history import TurnHistoryProjector
from app.services.infrastructure.turn_history import TurnHistoryStore
from tests.support.turn_history.checkpoints import (
    replace_with_compacted_checkpoint,
    seed_compactable_checkpoint,
)
from tests.support.turn_history.event_builders import (
    LONG_SESSION_TURN_COUNT,
    build_long_session_events,
    build_steering_merge_events,
    build_turn_events,
)
from tests.support.turn_history.projection import (
    rebuild_turn_projection,
    write_trace_fixture,
)


@dataclass(frozen=True, slots=True)
class SeededTurnSession:
    session_id: str
    workspace_root: Path
    events: list[Event]
    store: TurnHistoryStore
    projector: TurnHistoryProjector


@pytest.fixture(scope="module")
def seeded_turn_session(
    e2e_backend_process: object,
    client_base_url: str,
    e2e_workspace_root_path: str,
) -> SeededTurnSession:
    del e2e_backend_process
    workspace_root = Path(e2e_workspace_root_path)
    with httpx.Client(
        base_url=client_base_url,
        headers={"X-Local-Token": "local-dev-token"},
        timeout=30,
    ) as sync_client:
        response = sync_client.post(
            "/api/v1/sessions",
            json={"title": "Turn History E2E"},
        )
    assert response.status_code == 200, response.text
    session_id = response.json()["data"]["session_id"]
    events = build_long_session_events(session_id=session_id)
    store = TurnHistoryStore(workspace_root / ".boxteam" / "sessions")
    projector = TurnHistoryProjector(store)
    rebuilt_count = rebuild_turn_projection(
        store=store,
        session_id=session_id,
        events=events,
    )
    assert rebuilt_count == LONG_SESSION_TURN_COUNT
    write_trace_fixture(
        workspace_root=workspace_root,
        session_id=session_id,
        events=events,
    )
    seed_compactable_checkpoint(
        workspace_root=workspace_root,
        session_id=session_id,
    )

    artifact_dir = workspace_root.parent / "artifacts"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    (artifact_dir / "turn-history-fixture.json").write_text(
        json.dumps(
            {
                "session_id": session_id,
                "turn_count": LONG_SESSION_TURN_COUNT,
                "event_count": len(events),
                "latest_turn_id": f"job_turn_e2e_{LONG_SESSION_TURN_COUNT:04d}",
                "latest_contains_large_markdown": True,
                "checkpoint_is_compactable": True,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return SeededTurnSession(
        session_id=session_id,
        workspace_root=workspace_root,
        events=events,
        store=store,
        projector=projector,
    )


@pytest.mark.asyncio
async def test_bootstrap_is_bounded_and_identifies_latest_turn(
    client: httpx.AsyncClient,
    seeded_turn_session: SeededTurnSession,
) -> None:
    response = await client.get(
        f"/api/v1/sessions/{seeded_turn_session.session_id}/bootstrap"
    )
    assert response.status_code == 200, response.text
    assert len(response.content) < 32 * 1024
    assert "data:image/png;base64" not in response.text
    assert "inline_data_url" not in response.text

    bootstrap = response.json()["data"]
    latest = bootstrap["latest_turn"]
    assert latest["turn_id"] == f"job_turn_e2e_{LONG_SESSION_TURN_COUNT:04d}"
    assert latest["job_id"] == latest["turn_id"]
    assert latest["ordinal"] == LONG_SESSION_TURN_COUNT
    assert latest["items_view"] == "summary"
    assert latest["status"] == "completed"
    assert latest["preview_truncated"] is True
    assert latest["user_messages"][0]["content_truncated"] is True
    assert latest["user_messages"][0]["attachment_count"] == 1
    assert latest["source_message_count"] == 1
    assert bootstrap["older_cursor"]
    assert bootstrap["event_cursor"] == (
        f"evt_job_completed_e2e_{LONG_SESSION_TURN_COUNT:04d}"
    )
    assert bootstrap["projection_epoch"] == 1
    assert bootstrap["active_job_id"] is None
    assert bootstrap["active_jobs"] == []
    assert bootstrap["active_job_count"] == 0


@pytest.mark.asyncio
async def test_legacy_long_trace_bootstraps_latest_before_background_migration(
    client: httpx.AsyncClient,
    seeded_turn_session: SeededTurnSession,
) -> None:
    create_response = await client.post(
        "/api/v1/sessions",
        json={"title": "Legacy Turn Projection Migration E2E"},
    )
    assert create_response.status_code == 200, create_response.text
    session_id = create_response.json()["data"]["session_id"]
    events = build_long_session_events(session_id=session_id)
    write_trace_fixture(
        workspace_root=seeded_turn_session.workspace_root,
        session_id=session_id,
        events=events,
        build_turn_index=False,
    )
    seed_compactable_checkpoint(
        workspace_root=seeded_turn_session.workspace_root,
        session_id=session_id,
    )

    first_response = await client.get(f"/api/v1/sessions/{session_id}/bootstrap")
    assert first_response.status_code == 200, first_response.text
    assert len(first_response.content) < 32 * 1024
    first = first_response.json()["data"]
    assert first["projection_state"] == "partial"
    assert first["latest_turn"] is None
    assert first["older_cursor"] is None
    partial_epoch = first["projection_epoch"]

    ready: dict[str, object] | None = None
    migration_deadline = asyncio.get_running_loop().time() + 30
    while asyncio.get_running_loop().time() < migration_deadline:
        response = await client.get(f"/api/v1/sessions/{session_id}/bootstrap")
        assert response.status_code == 200, response.text
        candidate = response.json()["data"]
        if candidate["projection_state"] == "ready":
            ready = candidate
            break
        await asyncio.sleep(0.1)
    assert ready is not None, "Turn staging migration 在 30 秒内未发布 ready 投影"
    assert ready["projection_epoch"] > partial_epoch
    assert ready["latest_turn"]["turn_id"] == (
        f"job_turn_e2e_{LONG_SESSION_TURN_COUNT:04d}"
    )
    assert ready["older_cursor"]

    detail_response = await client.post(
        f"/api/v1/sessions/{session_id}/turns/details",
        json={"turn_ids": [ready["latest_turn"]["turn_id"]]},
    )
    assert detail_response.status_code == 200, detail_response.text
    latest_detail = detail_response.json()["data"]["items"][0]
    assert latest_detail["items_view"] == "full"
    assert f"TURN-E2E-{LONG_SESSION_TURN_COUNT:04d}-FINAL" in (
        latest_detail["final_response"]
    )

    page_response = await client.get(
        f"/api/v1/sessions/{session_id}/turns",
        params={"limit": 20},
    )
    assert page_response.status_code == 200, page_response.text
    page = page_response.json()["data"]
    assert page["projection_epoch"] == ready["projection_epoch"]
    assert [item["ordinal"] for item in page["items"]] == list(
        range(LONG_SESSION_TURN_COUNT, LONG_SESSION_TURN_COUNT - 20, -1)
    )
    assert page["has_more"] is True


@pytest.mark.asyncio
async def test_turn_pages_and_details_preserve_complete_job_boundaries(
    client: httpx.AsyncClient,
    seeded_turn_session: SeededTurnSession,
) -> None:
    session_id = seeded_turn_session.session_id
    first_response = await client.get(
        f"/api/v1/sessions/{session_id}/turns",
        params={"limit": 20},
    )
    assert first_response.status_code == 200, first_response.text
    first_page = first_response.json()["data"]
    assert [item["ordinal"] for item in first_page["items"]] == list(
        range(LONG_SESSION_TURN_COUNT, LONG_SESSION_TURN_COUNT - 20, -1)
    )
    assert first_page["has_more"] is True
    assert first_page["next_cursor"]
    assert all(item["items_view"] == "summary" for item in first_page["items"])
    assert all(item["job_id"] == item["turn_id"] for item in first_page["items"])
    assert all(item["source_message_count"] == 1 for item in first_page["items"])

    second_response = await client.get(
        f"/api/v1/sessions/{session_id}/turns",
        params={"limit": 20, "cursor": first_page["next_cursor"]},
    )
    assert second_response.status_code == 200, second_response.text
    second_page = second_response.json()["data"]
    assert [item["ordinal"] for item in second_page["items"]] == list(
        range(LONG_SESSION_TURN_COUNT - 20, LONG_SESSION_TURN_COUNT - 40, -1)
    )
    assert not {item["turn_id"] for item in first_page["items"]}.intersection(
        item["turn_id"] for item in second_page["items"]
    )

    detail_ids = [
        first_page["items"][0]["turn_id"],
        first_page["items"][1]["turn_id"],
        second_page["items"][0]["turn_id"],
        second_page["items"][1]["turn_id"],
    ]
    detail_response = await client.post(
        f"/api/v1/sessions/{session_id}/turns/details",
        json={"turn_ids": detail_ids},
    )
    assert detail_response.status_code == 200, detail_response.text
    detail_batch = detail_response.json()["data"]
    assert [item["turn_id"] for item in detail_batch["items"]] == detail_ids
    assert all(item["items_view"] == "full" for item in detail_batch["items"])
    assert all(item["final_response"] for item in detail_batch["items"])
    assert all(
        {event["type"] for event in item["items"]}
        >= {"tool_call_start", "tool_call_end", "text_start", "text_end"}
        for item in detail_batch["items"]
    )
    latest_detail = detail_batch["items"][0]
    assert (
        f"TURN-E2E-{LONG_SESSION_TURN_COUNT:04d}-FINAL"
        in latest_detail["final_response"]
    )
    assert latest_detail["user_messages"][0]["attachments"][0] == {
        "file_id": (
            f"boxteam-session://{session_id}/attachments/"
            f"turn-{LONG_SESSION_TURN_COUNT:04d}.png"
        ),
        "name": f"turn-{LONG_SESSION_TURN_COUNT:04d}.png",
        "content_type": "image/png",
    }
    serialized_latest_detail = json.dumps(latest_detail, ensure_ascii=False)
    assert "data:image" not in serialized_latest_detail
    assert "inline_data_url" not in serialized_latest_detail

    too_many = await client.post(
        f"/api/v1/sessions/{session_id}/turns/details",
        json={"turn_ids": [item["turn_id"] for item in first_page["items"][:5]]},
    )
    assert too_many.status_code == 422
    duplicated = await client.post(
        f"/api/v1/sessions/{session_id}/turns/details",
        json={"turn_ids": [detail_ids[0], detail_ids[0]]},
    )
    assert duplicated.status_code == 422
    unknown = await client.post(
        f"/api/v1/sessions/{session_id}/turns/details",
        json={"turn_ids": ["job_turn_does_not_exist"]},
    )
    assert unknown.status_code == 404


@pytest.mark.asyncio
async def test_cursor_survives_append_revision_and_checkpoint_compaction(
    client: httpx.AsyncClient,
    seeded_turn_session: SeededTurnSession,
) -> None:
    session_id = seeded_turn_session.session_id
    page_response = await client.get(
        f"/api/v1/sessions/{session_id}/turns",
        params={"limit": 5},
    )
    assert page_response.status_code == 200, page_response.text
    page = page_response.json()["data"]
    cursor = page["next_cursor"]
    assert cursor

    appended_events = build_turn_events(
        session_id=session_id,
        turn_index=LONG_SESSION_TURN_COUNT,
    )
    for event in appended_events:
        seeded_turn_session.projector.apply_event(session_id, event)

    before_compaction = await client.get(
        f"/api/v1/sessions/{session_id}/turns",
        params={"limit": 5, "cursor": cursor},
    )
    assert before_compaction.status_code == 200, before_compaction.text
    before_ordinals = [
        item["ordinal"] for item in before_compaction.json()["data"]["items"]
    ]
    assert before_ordinals == list(
        range(LONG_SESSION_TURN_COUNT - 5, LONG_SESSION_TURN_COUNT - 10, -1)
    )

    completion = next(
        event for event in appended_events if isinstance(event, JobCompletedEvent)
    )
    revised_completion = JobCompletedEvent(
        event_id="evt_job_completed_e2e_revised",
        job_id=completion.job_id,
        timestamp=completion.timestamp,
        payload=JobCompletedPayload(result="TURN-E2E-0049-REVISED"),
    )
    revised_turn = seeded_turn_session.projector.apply_event(
        session_id,
        revised_completion,
    )
    assert revised_turn is not None
    assert revised_turn.revision > 1

    saver = seed_compactable_checkpoint(
        workspace_root=seeded_turn_session.workspace_root,
        session_id=session_id,
    )
    compacted_checkpoint_id = replace_with_compacted_checkpoint(
        saver=saver,
        session_id=session_id,
    )
    assert compacted_checkpoint_id
    after_compaction = await client.get(
        f"/api/v1/sessions/{session_id}/turns",
        params={"limit": 5, "cursor": cursor},
    )
    assert after_compaction.status_code == 200, after_compaction.text
    assert [
        item["ordinal"] for item in after_compaction.json()["data"]["items"]
    ] == before_ordinals


@pytest.mark.asyncio
async def test_destructive_rebuild_returns_explicit_stale_cursor(
    client: httpx.AsyncClient,
    seeded_turn_session: SeededTurnSession,
) -> None:
    session_id = seeded_turn_session.session_id
    page_response = await client.get(
        f"/api/v1/sessions/{session_id}/turns",
        params={"limit": 4},
    )
    assert page_response.status_code == 200, page_response.text
    cursor = page_response.json()["data"]["next_cursor"]
    assert cursor

    rebuilt = seeded_turn_session.projector.rebuild_from_events(
        session_id,
        seeded_turn_session.events,
        destructive=True,
    )
    assert rebuilt == LONG_SESSION_TURN_COUNT

    stale_response = await client.get(
        f"/api/v1/sessions/{session_id}/turns",
        params={"limit": 4, "cursor": cursor},
    )
    assert stale_response.status_code == 409, stale_response.text
    detail = stale_response.json()["detail"]
    assert detail["code"] == "stale_turn_cursor"
    assert detail["session_id"] == session_id
    assert detail["cursor_epoch"] < detail["current_epoch"]
    assert detail["message"]

    invalid_response = await client.get(
        f"/api/v1/sessions/{session_id}/turns",
        params={"limit": 4, "cursor": "not-an-opaque-turn-cursor"},
    )
    assert invalid_response.status_code == 400


@pytest.mark.asyncio
async def test_steering_jobs_are_projected_as_one_execution_turn(
    client: httpx.AsyncClient,
    seeded_turn_session: SeededTurnSession,
) -> None:
    create_response = await client.post(
        "/api/v1/sessions",
        json={"title": "Steering Merge Turn E2E"},
    )
    assert create_response.status_code == 200, create_response.text
    session_id = create_response.json()["data"]["session_id"]
    events = build_steering_merge_events(session_id=session_id)
    rebuilt = rebuild_turn_projection(
        store=seeded_turn_session.store,
        session_id=session_id,
        events=events,
    )
    assert rebuilt == 1

    page_response = await client.get(
        f"/api/v1/sessions/{session_id}/turns",
        params={"limit": 20},
    )
    assert page_response.status_code == 200, page_response.text
    page = page_response.json()["data"]
    assert page["has_more"] is False
    assert [item["turn_id"] for item in page["items"]] == [
        "job_turn_e2e_0001"
    ]
    summary = page["items"][0]
    assert summary["source_message_ids"] == [
        "msg_turn_e2e_0001",
        "msg_turn_e2e_0002",
        "msg_turn_e2e_0003",
    ]
    assert summary["source_message_count"] == 3
    assert summary["merged_job_ids"] == [
        "job_turn_e2e_0002",
        "job_turn_e2e_0003",
    ]
    assert summary["merged_job_count"] == 2
    assert summary["user_message_count"] == 3

    detail_response = await client.post(
        f"/api/v1/sessions/{session_id}/turns/details",
        json={"turn_ids": ["job_turn_e2e_0001"]},
    )
    assert detail_response.status_code == 200, detail_response.text
    detail = detail_response.json()["data"]["items"][0]
    assert detail["source_message_ids"] == summary["source_message_ids"]
    assert detail["merged_job_ids"] == summary["merged_job_ids"]
    assert [message["message_id"] for message in detail["user_messages"]] == (
        summary["source_message_ids"]
    )
    assert "TURN-E2E-0001-FINAL" in detail["final_response"]

    hidden_response = await client.post(
        f"/api/v1/sessions/{session_id}/turns/details",
        json={"turn_ids": ["job_turn_e2e_0002"]},
    )
    assert hidden_response.status_code == 404, hidden_response.text


@pytest.mark.parametrize(
    "corruption_kind",
    ["manifest_json", "index_json", "epoch_mismatch", "timeline_jsonl"],
)
@pytest.mark.asyncio
async def test_corrupted_projection_fails_explicitly_without_history_fallback(
    client: httpx.AsyncClient,
    seeded_turn_session: SeededTurnSession,
    corruption_kind: str,
) -> None:
    create_response = await client.post(
        "/api/v1/sessions",
        json={"title": f"Corrupted Turn Projection {corruption_kind}"},
    )
    assert create_response.status_code == 200, create_response.text
    session_id = create_response.json()["data"]["session_id"]
    events = build_turn_events(
        session_id=session_id,
        turn_index=0,
        trace_delta_count=0,
    )
    assert rebuild_turn_projection(
        store=seeded_turn_session.store,
        session_id=session_id,
        events=events,
    ) == 1
    write_trace_fixture(
        workspace_root=seeded_turn_session.workspace_root,
        session_id=session_id,
        events=events,
    )
    seed_compactable_checkpoint(
        workspace_root=seeded_turn_session.workspace_root,
        session_id=session_id,
    )

    projection_root = (
        get_session_path_resolver(
            seeded_turn_session.workspace_root / ".boxteam" / "sessions"
        ).resolve_session_node(session_id)
        / "turn_history"
    )
    if corruption_kind == "manifest_json":
        (projection_root / "manifest.json").write_text(
            "{broken manifest",
            encoding="utf-8",
        )
    elif corruption_kind == "index_json":
        (projection_root / "index.json").write_text(
            "{broken index",
            encoding="utf-8",
        )
    elif corruption_kind == "epoch_mismatch":
        index_path = projection_root / "index.json"
        index = json.loads(index_path.read_text(encoding="utf-8"))
        index["projection_epoch"] += 1
        index_path.write_text(json.dumps(index), encoding="utf-8")
    else:
        (projection_root / "timeline.jsonl").write_text(
            "{broken timeline jsonl}\n",
            encoding="utf-8",
        )

    response = await client.get(f"/api/v1/sessions/{session_id}/bootstrap")
    assert response.status_code == 500, response.text
    payload = response.json()
    assert "data" not in payload
    detail = payload["detail"]
    assert detail["code"] == "turn_projection_corrupted"
    assert detail["session_id"] == session_id
    assert detail["message"]
    assert "TURN-E2E-0001" not in response.text
