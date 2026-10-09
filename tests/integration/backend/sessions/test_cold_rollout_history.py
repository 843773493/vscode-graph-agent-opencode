"""公开 child history 读取的零物化与损坏证据保护合同。"""

from __future__ import annotations

import hashlib
import shutil
from collections.abc import AsyncIterator

import httpx
import pytest
from fastapi import FastAPI

from app.api import session_turns
from app.api.deps import (
    get_request_id,
    get_session_turn_history_service,
    verify_local_token,
)
from app.core.session_thread_context import session_thread_owner_scope
from app.domain.itemized.enums import PayloadKind
from app.domain.itemized.mutation_intents import MutationIntentOwner
from app.services.infrastructure.rollout_context.storage.primitives import (
    RolloutNotMaterializedError,
)
from tests.integration.backend.sessions.test_thread_runtime_runner import (
    RuntimeCase,
    _child,
    _session,
)
from tests.integration.backend.sessions.test_thread_runtime_runner import (
    runner_case as runner_case,  # noqa: PLC0414
)
from tests.integration.backend.sessions.test_thread_runtime_runner import (
    runtime_case as runtime_case,  # noqa: PLC0414
)


@pytest.fixture
async def history_client(runtime_case: RuntimeCase) -> AsyncIterator[httpx.AsyncClient]:
    application = FastAPI()
    application.include_router(session_turns.router, prefix="/api/v1")
    application.dependency_overrides[get_session_turn_history_service] = (
        lambda: runtime_case.runner.container.session_turn_history_service
    )
    application.dependency_overrides[verify_local_token] = lambda: "local-test"
    application.dependency_overrides[get_request_id] = lambda: "request-cold-history"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application), base_url="http://test",
    ) as client:
        yield client


async def test_idle_child_public_history_does_not_materialize_rollout(
    runtime_case: RuntimeCase, history_client: httpx.AsyncClient,
) -> None:
    session_id, _main = await _session(runtime_case.runner)
    child = await _child(runtime_case, session_id)
    storage = runtime_case.runner.saver._storage.storage
    root = storage.root(session_id, thread_id=child)
    lock = root.parent / ".rollout.write.lock"
    assert not root.exists() and not lock.exists()

    response = await history_client.post(
        f"/api/v1/sessions/{session_id}/threads/{child}/history", json={},
    )

    assert response.status_code == 200, response.text
    page = response.json()["data"]
    assert page["thread_id"] == child
    assert page["items"] == page["summaries"] == page["internal_executions"] == []
    assert page["has_more"] is False and page["projection_epoch"] == 1
    saver = runtime_case.runner.saver
    with session_thread_owner_scope(MutationIntentOwner(session_id, child), langgraph_thread_id=child):
        config = {"configurable": {"thread_id": child}}
        assert saver.get_tuple(config) is None
        assert list(saver.list(config)) == []
        assert saver.resolve_latest_completed_turn_anchor(config) is None
    assert storage.read_items(session_id, child) == []
    assert saver._storage.executions.pending_runtime_wakeups(session_id, child) == ()
    assert saver._storage.assemblies.list_context_contributions(session_id, child) == ()
    assert saver._storage.assemblies.list_source_overlays(session_id, child) == ()
    assert saver._storage.checkpoints.read_context_source_control_states(session_id, thread_id=child) == ()
    assert saver._storage.checkpoints.read_context_source_rebuild_rows(session_id, child, checkpoint_ns="") is None
    with pytest.raises(RolloutNotMaterializedError):
        saver._storage.checkpoints.pending_writes(session_id, child, "", "checkpoint-missing")
    main_root = storage.root(session_id, thread_id=_main)
    assert storage.read_item_projections(session_id) == []
    with pytest.raises(KeyError, match="item projection 缺少请求的 canonical item: item-missing"):
        storage.read_item_projections(session_id, item_ids=("item-missing",))
    assert not main_root.exists() and not (main_root.parent / ".rollout.write.lock").exists()
    assert not root.exists() and not lock.exists()
    assert runtime_case.compiled == []


async def test_idle_child_history_validates_cursor_and_turn_references_without_writes(
    runtime_case: RuntimeCase, history_client: httpx.AsyncClient,
) -> None:
    session_id, _main = await _session(runtime_case.runner)
    child = await _child(runtime_case, session_id)
    root = runtime_case.runner.saver._storage.storage.root(session_id, thread_id=child)
    cases = [
        ({"cursor": "malformed"}, 400),
        ({"cursor": "malformed", "turn_ids": ["turn-missing"]}, 400),
        ({"turn_ids": ["turn-missing"]}, 409),
        ({"direction": "around"}, 422),
        ({"direction": "around", "anchor_turn_id": "turn-missing"}, 409),
    ]
    for request, status in cases:
        response = await history_client.post(
            f"/api/v1/sessions/{session_id}/threads/{child}/history", json=request,
        )
        assert response.status_code == status, response.text
        assert not root.exists() and not (root.parent / ".rollout.write.lock").exists()
    assert runtime_case.compiled == []


async def test_idle_child_history_rejects_dangling_rollout_path_without_writes(
    runtime_case: RuntimeCase, history_client: httpx.AsyncClient,
) -> None:
    session_id, _main = await _session(runtime_case.runner)
    child = await _child(runtime_case, session_id)
    root = runtime_case.runner.saver._storage.storage.root(session_id, thread_id=child)
    root.symlink_to("missing-rollout", target_is_directory=True)

    response = await history_client.post(
        f"/api/v1/sessions/{session_id}/threads/{child}/history", json={},
    )

    assert response.status_code == 500, response.text
    assert "recovery_required" in response.json()["detail"]["message"]
    assert root.is_symlink() and str(root.readlink()) == "missing-rollout"
    assert not root.exists() and not (root.parent / ".rollout.write.lock").exists()
    assert runtime_case.compiled == []


@pytest.mark.parametrize("missing", ["index.sqlite", "rollout.jsonl", "rollout"])
async def test_child_history_missing_authority_preserves_remaining_bytes(
    runtime_case: RuntimeCase, history_client: httpx.AsyncClient, missing: str,
) -> None:
    session_id, _main = await _session(runtime_case.runner)
    child = await _child(runtime_case, session_id)
    saver = runtime_case.runner.saver
    saver.accept_turn(
        session_id, thread_id=child, accepted_ingress_id="ingress-cold",
        acceptance_idempotency_key="acceptance-cold", job_id="job-cold",
        admission_idempotency_key="admission-cold", payload="不能被读请求销毁的历史",
        payload_kind=PayloadKind.TEXT, turn_id="turn-cold", root_item_id="item-cold",
        initial_execution_id="execution-cold",
    )
    root = saver._storage.storage.root(session_id, thread_id=child)
    good = await history_client.post(
        f"/api/v1/sessions/{session_id}/threads/{child}/history", json={},
    )
    assert good.status_code == 200 and good.json()["data"]["items"]
    original = {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                for path in root.iterdir() if path.is_file()}
    if missing == "rollout":
        shutil.rmtree(root)
        remaining = {}
    else:
        (root / missing).unlink()
        remaining = {name: digest for name, digest in original.items() if name != missing}

    response = await history_client.post(
        f"/api/v1/sessions/{session_id}/threads/{child}/history", json={},
    )

    assert response.status_code == 500, response.text
    assert "recovery_required" in response.json()["detail"]["message"]
    with pytest.raises(RuntimeError, match="recovery_required"):
        saver._storage.checkpoints.read_context_source_control_states(session_id, thread_id=child)
    with pytest.raises(RuntimeError, match="recovery_required"):
        saver._storage.checkpoints.read_context_source_rebuild_rows(session_id, child, checkpoint_ns="")
    with pytest.raises(RuntimeError, match="recovery_required"):
        saver._storage.checkpoints.pending_writes(session_id, child, "", "checkpoint-missing")
    actual = {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
              for path in root.iterdir() if path.is_file()} if root.exists() else {}
    assert actual == remaining
    assert not root.exists() if missing == "rollout" else not (root / missing).exists()
    assert runtime_case.compiled == []
