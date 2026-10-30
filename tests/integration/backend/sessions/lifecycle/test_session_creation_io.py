"""真实 fork 的 Session 创建在同步阶段取消后仍沿原 intent 恢复。"""

from __future__ import annotations

import asyncio
import sqlite3
import threading
from collections.abc import Awaitable
from contextlib import closing

import httpx
import pytest

from app import main
from app.container import AppContainer
from app.schemas.internal_v2.session import SessionDTO
from tests.integration.backend.sessions.fork_execution_source import (
    fork_execution_message as fork_execution_message,  # noqa: PLC0414
)
from tests.integration.backend.sessions.fork_execution_source import (
    fork_execution_source as fork_execution_source,  # noqa: PLC0414
)
from tests.integration.backend.sessions.test_rollout_fork_modes import (
    _fork_request,
    _main_thread_node,
    _production_fork_context,
)
from tests.integration.backend.sessions.test_rollout_fork_modes import (
    fork_gateway_request_context as fork_gateway_request_context,  # noqa: PLC0414
)
from tests.integration.backend.sessions.test_rollout_fork_modes import (
    production_fork_container as production_fork_container,  # noqa: PLC0414
)
from tests.unit.core.session_deletion.test_operation_replay_gc import (
    NativeStep,
    _entered,
    _turns,
)
from tests.unit.core.session_deletion.test_operation_replay_gc import (
    native_step as native_step,  # noqa: PLC0414
)


@pytest.mark.parametrize("fork_execution_source", [True], indirect=True)
@pytest.mark.parametrize("stage", ["freeze", "staging", "rename", "publish-before", "publish-after", "quarantine"])
async def test_product_session_creation_cancel_preserves_original_record_and_recovery(
    production_fork_container: AppContainer, fork_execution_source: SessionDTO,
    native_step: NativeStep, monkeypatch: pytest.MonkeyPatch, stage: str,
) -> None:
    context = _production_fork_context(production_fork_container)
    source = fork_execution_source
    request = await _fork_request(context, source.session_id, creation_kind="new_session_main")
    key = context.forks._creation_key(request.idempotency_key)
    resolver = context.sessions.path_resolver
    catalog = resolver.catalog_store
    creation = context.sessions._creation_service
    canonical = _main_thread_node(context, source.session_id) / "rollout/rollout.jsonl"
    source_bytes = canonical.read_bytes()
    method = {
        "freeze": "create_or_get_creation_record", "staging": "_prepare_staging",
        "rename": "_rename_staging_to_target", "publish-before": "publish_creation_record",
        "publish-after": "publish_creation_record", "quarantine": "_quarantine_prepared",
    }[stage]
    owner = catalog if stage in {"freeze", "publish-before", "publish-after"} else creation
    original = getattr(owner, method)
    blockers: list[sqlite3.Connection] = []
    loop_thread = threading.get_ident()

    def pause() -> None:
        if native_step.calls:
            return
        if stage in {"freeze", "publish-before"}:
            blocker = sqlite3.connect(catalog.database_path, isolation_level=None, check_same_thread=False)
            blocker.execute("BEGIN IMMEDIATE")
            blockers.append(blocker)
            native_step.calls.append(threading.get_ident())
            native_step.entered.set()
        else:
            native_step.block()

    def wait(*args, **kwargs):
        operation_key = kwargs.get("idempotency_key") if stage == "freeze" else (
            args[0] if stage in {"publish-before", "publish-after"} else args[0].session_creation_idempotency_key
        )
        if operation_key == key and stage != "publish-after":
            pause()
        result = original(*args, **kwargs)
        if operation_key == key and stage == "publish-after":
            pause()
        return result

    async def fail_materialization(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("产品创建完成后的物化故障")

    gates = [creation._key_lock(key), creation._gate.exclusive()]
    entered = [asyncio.Event() for _ in gates]

    async def contend(index: int) -> None:
        async with gates[index]:
            entered[index].set()

    def release() -> None:
        native_step.release.set()
        for blocker in blockers:
            if blocker.in_transaction:
                blocker.execute("ROLLBACK")

    async with main.lifespan(main.app), httpx.AsyncClient(
        transport=httpx.ASGITransport(app=main.app), base_url="http://workspace",
        headers={"X-Local-Token": "local-dev-token", "X-BoxTeam-Workspace-Id": source.workspace_id,
                 "X-BoxTeam-Gateway-Id": "gateway_6c8b2f9317a5a2c6c4569958b1e23d84",
                 "X-Request-ID": "session-creation-io"},
    ) as client:
        url = f"/api/v1/sessions/{source.session_id}/fork-context"
        tasks: list[Awaitable[object]] = []
        try:
            with monkeypatch.context() as patch:
                patch.setattr(owner, method, wait)
                if stage == "quarantine":
                    patch.setattr(context.forks, "_materialize", fail_materialization)
                creating = asyncio.create_task(client.post(url, json=request.model_dump(mode="json")))
                tasks.append(creating)
                try:
                    await _entered(native_step)
                    # 独立只读连接核对原件，不能在被测writer持有连接锁时从事件循环重入它。
                    with closing(sqlite3.connect(catalog.database_path)) as reader:
                        frozen = reader.execute(
                            "SELECT session_id,main_thread_id,storage_relative_locator,preimage_hash,state "
                            "FROM session_creation_records WHERE session_creation_idempotency_key=?", (key,),
                        ).fetchone()
                    if stage == "freeze":
                        assert frozen is None
                    else:
                        assert frozen is not None
                        assert frozen[4] == ("published" if stage == "publish-after" else "preparing")
                    tasks.extend(asyncio.create_task(contend(index)) for index in range(len(gates)))
                    creating.cancel("取消产品 Session 创建等待")
                    await _turns()
                    assert not creating.done() and not entered[0].is_set()
                    if stage in {"freeze", "publish-before", "publish-after"}:
                        assert not entered[1].is_set()
                    else:
                        await tasks[-1]
                        assert entered[1].is_set()
                    # 此fixture隔离了持久任务调度，使用独立工作区请求核对事件循环仍可响应。
                    workspace = await client.get("/api/v1/workspace", headers={"X-Request-ID": "session-creation-loop-probe"})
                    assert workspace.status_code == 200, workspace.text
                    assert workspace.headers["X-Request-ID"] == workspace.json()["request_id"] == "session-creation-loop-probe"
                    assert workspace.json()["data"]["workspace_id"] == source.workspace_id
                    assert canonical.read_bytes() == source_bytes
                    release()
                    with pytest.raises(asyncio.CancelledError):
                        await creating
                    await asyncio.gather(*tasks, return_exceptions=True)
                    assert all(event.is_set() for event in entered)
                    assert len(native_step.calls) == 1 and native_step.calls[0] != loop_thread
                finally:
                    release()
                    await asyncio.gather(*tasks, return_exceptions=True)
            record = catalog.get_creation_record(key)
            if frozen is not None:
                assert (record.session_id, record.main_thread_id, record.storage_relative_locator, record.preimage_hash) == frozen[:4]
            expected = "aborted" if stage == "quarantine" else "published" if stage.startswith("publish-") else "preparing"
            assert record.state == expected
            if stage == "quarantine":
                assert creation._orphaned_dir(record.session_id).is_dir()
                rejected = await client.post(url, json=request.model_dump(mode="json"))
                assert rejected.status_code == 500, rejected.text
                assert rejected.headers["X-Request-ID"] == rejected.json()["request_id"] == "session-creation-io"
                with pytest.raises(RuntimeError, match="fork operation 已中止"):
                    await context.forks.fork(request)
                assert catalog.get_creation_record(key) == record
                with pytest.raises(KeyError):
                    catalog.get_node(record.session_id)
            else:
                recovered = await client.post(url, json=request.model_dump(mode="json"))
                assert recovered.status_code == 200, recovered.text
                assert recovered.headers["X-Request-ID"] == recovered.json()["request_id"] == "session-creation-io"
                result = recovered.json()["data"]
                assert (result["session"]["session_id"], result["thread_id"]) == (record.session_id, record.main_thread_id)
                repeated = await client.post(url, json=request.model_dump(mode="json"))
                assert repeated.status_code == 200 and repeated.json()["data"] == result
                published = catalog.get_creation_record(key)
                assert published.state == "published" and published.abort_reason is None
                assert (published.session_id, published.main_thread_id, published.preimage_hash) == (
                    record.session_id, record.main_thread_id, record.preimage_hash,
                )
                assert (_main_thread_node(context, record.session_id) / "rollout/rollout.jsonl").read_bytes() == source_bytes
            assert canonical.read_bytes() == source_bytes
            assert catalog.connection.execute(
                "SELECT COUNT(*) FROM session_creation_records WHERE session_creation_idempotency_key=?", (key,),
            ).fetchone()[0] == 1
        finally:
            release()
            await asyncio.gather(*tasks, return_exceptions=True)
            for blocker in blockers:
                blocker.close()
            # 正式watchdog继续同一操作的恢复，不手工发布或中止目标。
            if stage == "quarantine":
                with pytest.raises(RuntimeError, match="fork operation 已中止"):
                    await context.forks.fork(request)
            else:
                recovered = await client.post(url, json=request.model_dump(mode="json"))
                assert recovered.status_code == 200, recovered.text
