"""删除已逻辑提交后的故障回执与恢复，须保留诊断但不保存原生位置。"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from contextlib import closing

import httpx
import pytest

from app.container import AppContainer
from app.core.identifier import create_prefixed_id
from app.core.session_catalog_store.contracts import SubtreeDeleteRecord
from app.schemas.internal_v2.session import SessionCreateRequest
from app.schemas.internal_v2.session_navigation.operations import (
    NavigationMutationReceiptDTO,
)
from tests.integration.backend.sessions.navigation.test_public_errors import (
    navigation_api as navigation_api,  # noqa: PLC0414
)
from tests.integration.backend.sessions.navigation.test_public_errors import (
    startup_container as startup_container,  # noqa: PLC0414
)
from tests.integration.backend.sessions.test_catalog_startup_identity import (
    _catalog_dump,
)

_PRIVATE = "/private/delete-settlement-source"
_WINDOWS = r"C:\private\delete-settlement-target"


async def _receipt(client: httpx.AsyncClient, operation_id: str) -> NavigationMutationReceiptDTO:
    response = await client.get(
        "/api/v1/session-catalog/operations", params={"operation_id": operation_id},
        headers={"X-Request-ID": "delete-settlement-request"},
    )
    assert response.status_code == 200, response.text
    assert response.headers["X-Request-ID"] == "delete-settlement-request"
    assert response.json()["request_id"] == "delete-settlement-request"
    assert _PRIVATE not in response.text and _WINDOWS not in response.text
    return NavigationMutationReceiptDTO.model_validate(response.json()["data"]["items"][0])


async def _settlement(
    client: httpx.AsyncClient, operation_id: str, expected: str,
) -> NavigationMutationReceiptDTO:
    async with asyncio.timeout(5):
        while True:
            receipt = await _receipt(client, operation_id)
            if receipt.settlement == expected:
                return receipt
            await asyncio.sleep(0.01)


@pytest.mark.parametrize("kind", ["delete_folder", "delete_session"])
@pytest.mark.parametrize("error_kind", ["runtime", "os", "sqlite"])
async def test_delete_settlement_failure_is_safe_durable_and_recoverable(
    navigation_api: tuple[AppContainer, httpx.AsyncClient, str],
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
    kind: str, error_kind: str,
) -> None:
    container, client, created_id = navigation_api
    catalog = container.session_catalog_service
    folder_id = (await _receipt(client, created_id)).created_node_id
    assert folder_id is not None
    session = await container.session_service.create(SessionCreateRequest(
        title="真实删除收尾", folder_id=str(folder_id) if kind == "delete_folder" else None,
    ))
    source = catalog.path_resolver.resolve_session_node(session.session_id)
    source_files = {
        path.relative_to(source): path.read_bytes() for path in source.rglob("*")
        if path.is_file() and path.suffix != ".sqlite" and not path.name.endswith(("-wal", "-shm"))
    }
    source_databases = {path.relative_to(source): _catalog_dump(path) for path in source.rglob("*.sqlite")}
    target = folder_id if kind == "delete_folder" else session.session_id
    operation_id = create_prefixed_id("op")
    failure: Exception
    if error_kind == "os":
        failure = PermissionError(13, "注入的原生文件故障", _PRIVATE, None, _WINDOWS)
        code = "errno=13"
    elif error_kind == "sqlite":
        with closing(sqlite3.connect(":memory:")) as connection, pytest.raises(sqlite3.OperationalError) as caught:
            connection.execute(f'SELECT * FROM "{_PRIVATE}"')
        failure = caught.value
        code = "sqlite_errorcode=1"
    else:
        failure = RuntimeError(f"注入的收尾故障: {_PRIVATE}")
        failure.__cause__ = OSError(5, "保留底层异常链", _WINDOWS)
        code = "error=RuntimeError"

    started, release = asyncio.Event(), asyncio.Event()
    delete_service = catalog.path_resolver._delete_service
    original_drain = delete_service._drain

    async def fail_drain(record: SubtreeDeleteRecord, idempotency_key: str) -> None:
        assert idempotency_key == operation_id and record.root_node_id == target
        started.set()
        await release.wait()
        raise failure

    # 仅在真实 mark 提交之后注入物理排空故障，不预造队列、journal 或 session。
    monkeypatch.setattr(delete_service, "_drain", fail_drain)
    caplog.set_level(logging.ERROR)
    try:
        accepted = await client.post("/api/v1/session-catalog/operations:enqueue", json={
            "intents": [{
                "client_operation_id": operation_id, "client_sequence": 1,
                "kind": kind, "target_node_id": target,
                "recursive": kind == "delete_folder",
                "base_catalog_revision": catalog.path_resolver.revision,
            }],
        })
        assert accepted.status_code == 202, accepted.text
        await asyncio.wait_for(started.wait(), timeout=5)
        pending = await _receipt(client, operation_id)
        assert pending.state == "committed" and pending.settlement == "pending"
        release.set()
        failed = await _settlement(client, operation_id, "repair_required")
        assert failed.state == "committed"
        assert failed.error_code == "delete_settlement_repair_required"
        detail = str(failed.error_detail)
        for expected in (operation_id, str(target), "artifact=session-subtree", "stage=delete_settlement", type(failure).__name__, code):
            assert expected in detail
        assert failed.receipt_revision > pending.receipt_revision
        store = catalog.path_resolver.catalog_store
        journal = store.get_subtree_delete_record(operation_id)
        assert journal.state == "deleting"
        assert source.is_dir()
        assert {
            path.relative_to(source): path.read_bytes() for path in source.rglob("*")
            if path.is_file() and path.suffix != ".sqlite" and not path.name.endswith(("-wal", "-shm"))
        } == source_files
        assert {path.relative_to(source): _catalog_dump(path) for path in source.rglob("*.sqlite")} == source_databases
        with store.read_transaction() as connection:
            row = connection.execute(
                "SELECT state, pending_settlement, error_detail FROM navigation_mutation_records WHERE operation_id=?", (operation_id,),
            ).fetchone()
            assert tuple(row) == ("committed", 1, detail)
            assert connection.execute("SELECT state FROM nodes WHERE node_id=?", (target,)).fetchone()[0] == "deleting"
        dump = _catalog_dump(store.database_path)
        for private in (_PRIVATE, _WINDOWS, str(container.workspace_root), str(store.database_path)):
            assert private not in detail and private not in dump
        diagnostics = [record for record in caplog.records if record.name.endswith("session_navigation.executor") and record.exc_info]
        assert any(record.exc_info[1] is failure for record in diagnostics)
        assert str(failure) in caplog.text and _PRIVATE in caplog.text
        if failure.__cause__ is not None:
            assert str(failure.__cause__) in caplog.text

        events = await client.get("/api/v1/session-catalog/navigation-events")
        assert events.status_code == 200, events.text
        committed_event = next(item for item in events.json()["data"]["items"] if item["operation_id"] == operation_id)
        assert committed_event["result_state"] == "committed"
        assert committed_event["error_detail"] is None

        await catalog.operations.stop()
        await catalog.operations.start()
        repeated = await _settlement(client, operation_id, "repair_required")
        assert repeated.operation_id == operation_id
        assert repeated.receipt_revision == failed.receipt_revision
        assert repeated.error_detail == failed.error_detail
        assert store.get_subtree_delete_record(operation_id).frozen_node_ids == journal.frozen_node_ids

        monkeypatch.setattr(delete_service, "_drain", original_drain)
        await catalog.operations.stop()
        await catalog.operations.start()
        settled = await _settlement(client, operation_id, "settled")
        assert settled.state == "committed" and settled.operation_id == operation_id
        assert settled.receipt_revision > failed.receipt_revision
        assert settled.error_code is None and settled.error_detail is None
        finished = store.get_subtree_delete_record(operation_id)
        assert finished.state == "completed" and finished.frozen_node_ids == journal.frozen_node_ids
        assert not source.exists()
        final_events = await client.get("/api/v1/session-catalog/navigation-events")
        assert final_events.status_code == 200, final_events.text
        assert [item for item in final_events.json()["data"]["items"] if item["operation_id"] == operation_id] == [committed_event]
    finally:
        release.set()
