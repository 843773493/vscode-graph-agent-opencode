"""公开 child history 读取的零物化与损坏证据保护合同。"""

from __future__ import annotations

import asyncio
import hashlib
import os
import shutil
import threading
from collections.abc import AsyncIterator
from concurrent.futures import ThreadPoolExecutor

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
    _RolloutFileLock,
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


@pytest.fixture
async def materialized_history_owner(runtime_case: RuntimeCase) -> tuple[str, str]:
    session_id, thread_id = await _session(runtime_case.runner)
    runtime_case.runner.saver.accept_turn(
        session_id, thread_id=thread_id, accepted_ingress_id="ingress-await-safe-history",
        acceptance_idempotency_key="acceptance-await-safe-history", job_id="job-await-safe-history",
        admission_idempotency_key="admission-await-safe-history", payload="真实异步历史读取",
        payload_kind=PayloadKind.TEXT, turn_id="turn-await-safe-history", root_item_id="item-await-safe-history",
        initial_execution_id="execution-await-safe-history",
    )
    return session_id, thread_id


async def _history_request(
    client: httpx.AsyncClient, session_id: str, thread_id: str, entry: str,
) -> httpx.Response:
    if entry == "bootstrap":
        return await client.get(f"/api/v1/sessions/{session_id}/bootstrap")
    return await client.post(f"/api/v1/sessions/{session_id}/threads/{thread_id}/history", json={})


async def _wait_for_rollout_readers(lock: _RolloutFileLock, size: int) -> None:
    admission = lock._admission
    assert admission is not None
    async with asyncio.timeout(2):
        while True:
            with admission._condition:
                if len(admission._queue) == size:
                    return
            await asyncio.sleep(0)


@pytest.mark.skipif(os.name != "posix", reason="公开历史入口与真实 POSIX 读写锁交错")
@pytest.mark.parametrize("entry", ["bootstrap", "history"])
async def test_public_history_awaits_writer_without_blocking_holder_task(
    runtime_case: RuntimeCase, history_client: httpx.AsyncClient,
    materialized_history_owner: tuple[str, str], entry: str,
) -> None:
    session_id, thread_id = materialized_history_owner
    storage = runtime_case.runner.saver._storage.storage
    original = storage.jsonl_path(session_id, thread_id=thread_id).read_bytes()
    holder = storage.read_lock(session_id, thread_id)
    await holder.acquire_async()
    writer_ready = threading.Event()
    release_writer = threading.Event()

    def write() -> None:
        with storage._lock(thread_id, "", session_id=session_id):
            writer_ready.set()
            assert release_writer.wait(timeout=2)

    request: asyncio.Task[httpx.Response] | None = None
    with ThreadPoolExecutor(max_workers=1) as executor:
        writer = executor.submit(write)
        try:
            await _wait_for_rollout_readers(holder, 1)
            request = asyncio.create_task(_history_request(history_client, session_id, thread_id, entry))
            await _wait_for_rollout_readers(holder, 2)
            assert not request.done() and not writer_ready.is_set()
            holder.release()
            assert await asyncio.to_thread(writer_ready.wait, 1)
            assert not request.done()
            release_writer.set()
            response = await asyncio.wait_for(request, timeout=2)
            writer.result(timeout=1)
            assert response.status_code == 200, response.text
            data = response.json()["data"]
            assert data["thread_id"] == thread_id
            if entry == "bootstrap":
                assert data["latest_turn"]["turn_id"] == "turn-await-safe-history"
            else:
                assert data["items"][0]["turn_id"] == "turn-await-safe-history"
        finally:
            holder.release()
            release_writer.set()
            if request is not None:
                request.cancel()
                await asyncio.gather(request, return_exceptions=True)
    # 已物化 snapshot 的游标异常也必须释放读 lease，后续真实写者仍能进入。
    invalid = await history_client.post(
        f"/api/v1/sessions/{session_id}/threads/{thread_id}/history", json={"cursor": "malformed"},
    )
    assert invalid.status_code == 400, invalid.text

    def probe_after_error() -> None:
        with storage._lock(thread_id, "", session_id=session_id):
            pass

    with ThreadPoolExecutor(max_workers=1) as executor:
        executor.submit(probe_after_error).result(timeout=1)
    assert storage.jsonl_path(session_id, thread_id=thread_id).read_bytes() == original


@pytest.mark.parametrize("entry", ["bootstrap", "history"])
async def test_public_history_cancellation_withdraws_read_admission(
    runtime_case: RuntimeCase, history_client: httpx.AsyncClient,
    materialized_history_owner: tuple[str, str], entry: str,
) -> None:
    session_id, thread_id = materialized_history_owner
    storage = runtime_case.runner.saver._storage.storage
    async with storage._lock(thread_id, "", session_id=session_id) as holder:
        admission = holder._admission
        assert admission is not None
        request = asyncio.create_task(_history_request(history_client, session_id, thread_id, entry))
        try:
            await _wait_for_rollout_readers(holder, 1)
            request.cancel()
            with pytest.raises(asyncio.CancelledError):
                await request
            assert not admission._queue and len(admission._holders) == 1
        finally:
            request.cancel()
            await asyncio.gather(request, return_exceptions=True)
    assert not admission._holders
    response = await _history_request(history_client, session_id, thread_id, entry)
    assert response.status_code == 200, response.text


async def test_idle_child_public_history_does_not_materialize_rollout(
    runtime_case: RuntimeCase, history_client: httpx.AsyncClient,
) -> None:
    session_id, _main = await _session(runtime_case.runner)
    child = await _child(runtime_case, session_id)
    storage = runtime_case.runner.saver._storage.storage
    runtime_case.runner.saver.accept_turn(
        session_id, thread_id=_main, accepted_ingress_id="main-ingress",
        acceptance_idempotency_key="main-acceptance", job_id="main-job",
        admission_idempotency_key="main-admission", payload="主线程已有的权威历史",
        turn_id="main-turn", root_item_id="main-root",
    )
    main_root = storage.root(session_id, thread_id=_main)
    main_lock = main_root.parent / ".rollout.write.lock"
    # 由真实 acceptance 初始化 main；child 只读入口必须保留这份权威数据。
    assert main_root.is_dir() and main_lock.is_file()
    main_authority = (main_root / "index.sqlite", main_root / "rollout.jsonl", main_lock)
    main_before = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in main_authority}
    main_projections = storage.read_item_projections(session_id)
    assert main_projections
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
        assert [item async for item in saver.alist(config)] == []
        assert saver.resolve_latest_completed_turn_anchor(config) is None
    assert storage.read_items(session_id, child) == []
    assert saver._storage.executions.pending_runtime_wakeups(session_id, child) == ()
    assert saver._storage.assemblies.list_context_contributions(session_id, child) == ()
    assert saver._storage.assemblies.list_source_overlays(session_id, child) == ()
    assert saver._storage.checkpoints.read_context_source_control_states(session_id, thread_id=child) == ()
    assert saver._storage.checkpoints.read_context_source_rebuild_rows(session_id, child, checkpoint_ns="") is None
    with pytest.raises(RolloutNotMaterializedError):
        saver._storage.checkpoints.pending_writes(session_id, child, "", "checkpoint-missing")
    assert storage.read_item_projections(session_id) == main_projections
    with pytest.raises(KeyError, match="item projection 缺少请求的 canonical item: item-missing"):
        storage.read_item_projections(session_id, item_ids=("item-missing",))
    assert {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in main_authority} == main_before
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
    assert str(root) not in response.text
    assert str(root.parent) not in response.text
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
    assert str(root) not in response.text
    assert str(root.parent) not in response.text
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
