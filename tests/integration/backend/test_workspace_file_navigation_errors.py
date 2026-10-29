"""经真实文件 owner 与 ASGI 路由验证原生错误、受理及原请求重试。"""

from __future__ import annotations

import asyncio
import errno
import json
import logging
import os
import sqlite3
from contextlib import closing
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from uuid import uuid4

import anyio
import httpx
import pytest
from fastapi import FastAPI, HTTPException

from app.api import resources as resources_api
from app.api import workspace as workspace_api
from app.api.deps import get_text_mutations, get_workspace_service, verify_local_token
from app.core import failures
from app.core.trace_middleware import TraceMiddleware
from app.main import workspace_http_exception_handler
from app.services.infrastructure.config_service import ConfigService
from app.services.infrastructure.gateway_client_common import GatewayTransportConnection
from app.services.infrastructure.workspace_files import filesystem, operations
from app.services.infrastructure.workspace_files import paths as workspace_paths
from app.services.infrastructure.workspace_files.filesystem import (
    FileEffectUnverifiedError,
)
from app.services.infrastructure.workspace_files.paths import (
    GatewayNativeDirectoryClient,
)
from app.services.infrastructure.workspace_files.text_owner import (
    WorkspaceTextMutationOwner,
)
from app.services.infrastructure.workspace_service import WorkspaceService
from tests.harness.python.run_context import TestRunContext
from tests.support.workspaces import prepare_default_test_workspace


@pytest.fixture
async def file_case(request: pytest.FixtureRequest, integration_workspace_root_path: str):
    context = TestRunContext.from_test_file(Path(__file__)).prepare()
    # 每个场景重建完整模板，不能让前一场景的外部目录观察要求下一场景恢复其绑定。
    root = prepare_default_test_workspace(
        workspace_root=Path(integration_workspace_root_path),
        template_root=Path.cwd() / "tests/fixtures/workspaces/default_test_workspace",
        shared_skill_root=Path.cwd() / "resources/skills",
    )
    directory = root / ("navigation-" + uuid4().hex)
    directory.mkdir()
    (directory / "seed.txt").write_bytes(b"original\n")
    config = ConfigService(workspace_root=root, config_dir=context.runtime_root_for_node(request.node.nodeid) / "config")
    service = WorkspaceService(config_service=config, workspace_root=root)
    app = FastAPI()
    app.add_middleware(TraceMiddleware)
    app.add_exception_handler(HTTPException, workspace_http_exception_handler)
    app.include_router(workspace_api.router, prefix="/api/v1")
    app.include_router(resources_api.router, prefix="/api/v1")
    text_owner = WorkspaceTextMutationOwner(paths=service.file_paths)
    app.dependency_overrides[get_text_mutations] = lambda: text_owner
    app.dependency_overrides[get_workspace_service] = lambda: service
    app.dependency_overrides[verify_local_token] = lambda: "local-test"
    await service.file_operations.start()
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://files.test") as client:
            listed = await client.get("/api/v1/workspace/files", params={"path": str(directory)})
            assert listed.status_code == 200, listed.text
            yield SimpleNamespace(root=root, directory=directory, service=service, client=client,
                                  listing=listed.json()["data"], artifacts=context.artifacts_dir)
    finally:
        await service.file_operations.stop()
        service.close()
        await config.close()


def file_rows(case):
    with closing(sqlite3.connect(case.root / ".boxteam/navigation/workspace-files.sqlite")) as db:
        return {name: db.execute(f"SELECT * FROM {name}").fetchall()
                for name in ("owner", "directories", "operations", "events")}


@pytest.mark.skipif(os.name == "nt", reason="真实符号链接循环在 POSIX 目标验收")
async def test_patch_resolution_failure_preserves_receipt_and_allows_explicit_retry(
    file_case, native_control_sink, caplog: pytest.LogCaptureFixture,
):
    case = file_case
    loop = case.directory / "loop"
    loop.symlink_to(loop.name, target_is_directory=True)
    before = file_rows(case)
    payload = {"patch": {
        "target": case.listing["path"],
        "input": "*** Begin Patch\n*** Add File: loop/new.txt\n+created\n+\n*** End Patch",
        "expected_versions": {"loop/new.txt": "absent"}, "explanation": "验证原目标恢复",
    }}
    failed = await case.client.post("/api/v1/resources/text-mutations", json=payload,
                                   headers={"X-Request-ID": "req-patch-loop"})
    assert failed.status_code == 200, failed.text
    assert failed.json()["request_id"] == failed.headers["X-Request-ID"] == "req-patch-loop"
    receipt = failed.json()["data"]["receipt"]
    assert (receipt["status"], receipt["effect"], receipt["code"], receipt["owner"]) == (
        "error", "not_executed", "execution_failed", "resource_file_operations",
    )
    assert receipt["error_type"] in {"RuntimeError", "OSError"}
    assert str(case.root) not in failed.text
    assert not receipt["journal_id"] and not receipt["files"]
    assert file_rows(case) == before and loop.is_symlink()
    assert str(loop) in caplog.text or native_control_sink is not None
    if native_control_sink is not None:
        assert native_control_sink.errors
        assert str(loop) in str(native_control_sink.errors[0])
        assert "diagnostic_sink_error=" in receipt["error"]
        assert any(str(native_control_sink.blocked) in note for note in native_control_sink.errors[0].__notes__)
    loop.unlink()
    saved = await case.client.post("/api/v1/resources/text-mutations", json=payload)
    assert saved.status_code == 200, saved.text
    result = saved.json()["data"]["receipt"]
    assert (result["status"], result["effect"]) == ("success", "completed")
    assert result["files"][0]["path"] == case.listing["path"] + "/loop/new.txt"
    assert (loop / "new.txt").read_bytes() == b"created\n"
    assert (case.directory / "seed.txt").read_bytes() == b"original\n"


@pytest.fixture(params=[False, True], ids=["logging", "broken-logging"])
def native_control_sink(file_case, request: pytest.FixtureRequest):
    if not request.param:
        yield None
        return
    blocked = file_case.root / "blocked-native-control-log"
    blocked.write_bytes(b"keep obstruction")
    errors: list[BaseException] = []

    class FailingHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            if record.exc_info and record.exc_info[1] is not None:
                errors.append(record.exc_info[1])
            with (blocked / "service.log").open("a", encoding="utf-8"):
                pass

    handler = FailingHandler(level=logging.ERROR)
    loggers = (logging.getLogger(workspace_paths.__name__), failures.logger)
    for logger in loggers:
        logger.addHandler(handler)
    try:
        yield SimpleNamespace(blocked=blocked, errors=errors)
    finally:
        for logger in loggers:
            logger.removeHandler(handler)
        handler.close()
        assert blocked.read_bytes() == b"keep obstruction"


@pytest.mark.parametrize("operation", ["bind", "resolve"])
@pytest.mark.parametrize("invalid", ["data", "json", "extra_key", "code", "null_data", "request_id"])
async def test_native_control_invalid_reply_preserves_files_and_allows_original_request_retry(
    file_case, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
    operation: str, invalid: str, native_control_sink,
):
    case = file_case
    external = case.root.parent / ("private-native-root-" + uuid4().hex)
    external.mkdir()
    seed = external / "outside.txt"
    seed.write_bytes(b"external original\n")
    uri = "boxteam://gateway/gateway_01900000000070008000000000000001/resources/files/01900000000070008000000000000001"
    request_id = "req-native-" + operation + "-" + invalid
    requests, scanned = [], []
    broken = True

    def respond(request):
        requests.append(request)
        data: dict[str, object] = {"uri": uri}
        if request.method == "GET":
            data["native_root"] = str(external)
        payload: dict[str, object] = {"data": data, "request_id": request.headers["X-Request-ID"], "message": str(external)}
        if broken:
            if invalid == "data":
                data["uri" if operation == "bind" else "native_root"] = {"private-native-root": str(external)}
            elif invalid == "extra_key":
                data[str(external)] = str(external)
            elif invalid == "code":
                payload["code"] = 9
            elif invalid == "null_data":
                payload["data"] = None
            elif invalid == "request_id":
                payload["request_id"] = "req-other"
        content = ("invalid-json " + str(external)).encode() if broken and invalid == "json" else json.dumps(payload).encode()
        return httpx.Response(200, headers={"X-Request-ID": request.headers["X-Request-ID"]}, content=content)

    original_client, scandir = httpx.Client, os.scandir
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: original_client(
        **kwargs, transport=httpx.MockTransport(respond)))
    case.service.file_paths.bindings = GatewayNativeDirectoryClient(workspace_id=case.service.workspace_id,
        connection=GatewayTransportConnection(gateway_url="http://127.0.0.1:45123"),
        local_token_supplier=lambda: "local-token")

    def observe(path):
        if Path(path) == external:
            scanned.append(path)
        return scandir(path)

    monkeypatch.setattr(os, "scandir", observe)
    params = {"path": str(external) if operation == "bind" else uri}
    before = file_rows(case)
    failed = await case.client.get("/api/v1/workspace/files", params=params, headers={"X-Request-ID": request_id})
    assert failed.status_code == 400, failed.text
    failure = failed.json()
    assert failure["request_id"] == failed.headers["X-Request-ID"] == request_id
    assert "private-native-root" not in failed.text and str(case.root) not in failed.text
    stage = "response" if invalid in {"data", "json", "extra_key"} else "envelope"
    assert f"operation={operation}" in failure["detail"] and f"stage={stage}" in failure["detail"]
    if stage == "response":
        assert "error_count=1" in failure["detail"]
    assert file_rows(case) == before and not scanned
    assert seed.read_bytes() == b"external original\n"
    assert (case.directory / "seed.txt").read_bytes() == b"original\n"
    assert len(requests) == 1 and requests[0].method == ("POST" if operation == "bind" else "GET")
    if native_control_sink is None:
        assert str(external) in caplog.text
    else:
        assert "diagnostic_sink_error=NotADirectoryError" in failure["detail"]
        assert len(native_control_sink.errors) == 1
        assert any(str(external) in note for note in native_control_sink.errors[0].__notes__)
        assert any(str(native_control_sink.blocked) in note for note in native_control_sink.errors[0].__notes__)
    broken = False
    continued = await case.client.get("/api/v1/workspace/files", params=params, headers={"X-Request-ID": request_id})
    assert continued.status_code == 200, continued.text
    body = continued.json()
    assert body["request_id"] == continued.headers["X-Request-ID"] == request_id
    assert body["data"]["path"] == uri and scanned
    assert [(item["name"], item["path"]) for item in body["data"]["items"]] == [("outside.txt", uri + "/outside.txt")]
    assert "private-native-root" not in continued.text and str(case.root) not in continued.text
    assert seed.read_bytes() == b"external original\n"
    assert requests[0].url == requests[1].url and requests[0].content == requests[1].content
    (case.artifacts / f"native-control-{operation}-{invalid}-{'broken-logging' if native_control_sink else 'logging'}.json").write_text(json.dumps({
        "failure": failure, "continued": body,
    }, ensure_ascii=False, indent=2) + "\n")


async def test_directory_native_messages_stay_in_logs_and_original_listing_survives(
    file_case, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
):
    case = file_case
    before = file_rows(case)
    scandir = os.scandir
    evidence = []
    for number, status in ((errno.EACCES, 403), (errno.ENOENT, 404), (errno.ENOTDIR, 400)):
        for filename_field in ("first", "second", "none"):
            original = OSError(number, f"private-strerror: {case.directory}")
            # CPython 在 filename=None 时会忽略构造参数 filename2；分别设置以覆盖两个真实属性。
            if filename_field == "first":
                original.filename = str(case.directory)
            elif filename_field == "second":
                original.filename2 = str(case.directory / "private-second")

            def fail(path, error=original):
                if Path(path) == case.directory:
                    raise error
                return scandir(path)

            request_id = "req-file-navigation-" + str(len(evidence))
            with monkeypatch.context() as patch:
                patch.setattr(os, "scandir", fail)
                failed = await case.client.get("/api/v1/workspace/files", params={"path": case.listing["path"]},
                                               headers={"X-Request-ID": request_id})
            assert failed.status_code == status, failed.text
            body = failed.json()
            assert body["request_id"] == failed.headers["X-Request-ID"] == request_id
            assert str(case.directory) not in failed.text and "private-strerror" not in failed.text
            assert type(original).__name__ in body["detail"] and f"errno={number}" in body["detail"]
            assert "stage=目录扫描" in body["detail"] and case.listing["path"] in body["detail"]
            assert any(record.exc_info and record.exc_info[1] is original for record in caplog.records)
            assert file_rows(case) == before
            evidence.append(body)
    assert "private-strerror" in caplog.text and str(case.directory) in caplog.text
    continued = await case.client.get("/api/v1/workspace/files", params={"path": case.listing["path"]})
    assert continued.json()["data"] == case.listing
    assert (case.directory / "seed.txt").read_bytes() == b"original\n"
    (case.artifacts / "directory-errors.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n")


async def test_real_upload_staging_collision_keeps_409_original_state_and_same_id_retry(
    file_case, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
):
    case = file_case
    operation_id = "op_" + uuid4().hex
    fixed = uuid4()
    collision = case.root / ".boxteam/navigation/workspace-files-staging" / (operation_id + "-" + fixed.hex)
    collision.mkdir(parents=True)
    marker = collision / "foreign.txt"
    marker.write_bytes(b"do not remove\n")
    intent = {"client_operation_id": operation_id, "client_sequence": 1,
              "directory_key": case.listing["directory_key"],
              "expected_directory_revision": case.listing["directory_revision"]}
    before = file_rows(case)

    async def upload():
        return await case.client.post("/api/v1/workspace/files/upload", params={"path": case.listing["path"]},
            data={"intent": json.dumps(intent), "relative_paths": "uploaded.txt"},
            files={"files": ("uploaded.txt", b"payload\n", "text/plain")},
            headers={"X-Request-ID": "req-upload-collision"})

    with monkeypatch.context() as patch:
        patch.setattr(operations, "uuid4", lambda: fixed)
        failed = await upload()
    assert failed.status_code == 409, failed.text
    body = failed.json()
    assert body["request_id"] == failed.headers["X-Request-ID"] == "req-upload-collision"
    assert "FileExistsError" in body["detail"] and f"errno={errno.EEXIST}" in body["detail"]
    assert str(collision) not in failed.text and str(case.root) not in failed.text
    assert str(collision) in caplog.text
    assert file_rows(case) == before
    assert marker.read_bytes() == b"do not remove\n"
    assert not (case.directory / "uploaded.txt").exists()
    marker.unlink()
    collision.rmdir()
    accepted = await upload()
    assert accepted.status_code == 202, accepted.text
    async with asyncio.timeout(10):
        while True:
            result = await case.client.get("/api/v1/workspace/files/operations", params={
                "operation_id": operation_id, "expected_workspace_id": case.service.workspace_id,
            })
            assert result.status_code == 200, result.text
            receipt = result.json()["data"]["operations"][0]
            assert receipt["state"] in {"queued", "running", "committed"}, receipt
            if receipt["state"] == "committed":
                break
            await asyncio.sleep(0.02)
    assert receipt["effect"] == "complete"
    assert (case.directory / "uploaded.txt").read_bytes() == b"payload\n"
    assert (case.directory / "seed.txt").read_bytes() == b"original\n"
    (case.artifacts / "upload-collision.json").write_text(json.dumps({
        "failure": body, "accepted": accepted.json(), "receipt": receipt,
    }, ensure_ascii=False, indent=2) + "\n")


@pytest.mark.parametrize("filename_field", ["first", "second", "none"])
async def test_text_preflight_uses_shared_error_and_creates_nothing_until_retry(
    file_case, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
    filename_field: str,
):
    case = file_case
    target = case.directory / "new.txt"
    uri = case.listing["path"] + "/new.txt"
    original = OSError(errno.EIO, f"private-strerror: {target}")
    if filename_field == "first":
        original.filename = str(target)
    elif filename_field == "second":
        original.filename2 = str(target)
    resolve = Path.resolve
    before = file_rows(case)
    payload = {"create": {"target": uri, "content": "text payload\n", "expected_version": "absent"}}

    def fail(path, *args, **kwargs):
        if path == target:
            raise original
        return resolve(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "resolve", fail)
        failed = await case.client.post("/api/v1/resources/text-mutations", json=payload,
                                       headers={"X-Request-ID": "req-text-preflight"})
    assert failed.status_code == 200, failed.text
    body = failed.json()
    assert body["request_id"] == failed.headers["X-Request-ID"] == "req-text-preflight"
    receipt = body["data"]["receipt"]
    assert (receipt["status"], receipt["effect"], receipt["code"], receipt["owner"]) == (
        "error", "not_executed", "execution_failed", "resource_file_operations",
    )
    assert receipt["error_type"] == "OSError" and f"errno={errno.EIO}" in receipt["error"]
    assert "stage=目标解析" in receipt["error"] and uri in receipt["error"]
    assert not receipt["journal_id"] and not receipt["files"]
    assert str(case.root) not in failed.text and "private-strerror" not in failed.text
    assert any(record.exc_info and record.exc_info[1] is original for record in caplog.records)
    assert str(target) in caplog.text and "private-strerror" in caplog.text
    assert file_rows(case) == before and not target.exists()
    saved = await case.client.post("/api/v1/resources/text-mutations", json=payload)
    assert saved.status_code == 200, saved.text
    result = saved.json()["data"]["receipt"]
    assert (result["status"], result["effect"]) == ("success", "completed")
    assert result["files"][0]["path"] == uri and result["files"][0]["state"] == "published"
    assert target.read_bytes() == b"text payload\n"
    assert (case.directory / "seed.txt").read_bytes() == b"original\n"
    (case.artifacts / "text-preflight.json").write_text(json.dumps({
        "failure": body, "saved": saved.json(),
    }, ensure_ascii=False, indent=2) + "\n")


@pytest.mark.parametrize("failure_kind", ["denied", "conflict", "unverified"])
async def test_operation_log_failure_preserves_settlement_effect_and_fifo(
    file_case, monkeypatch: pytest.MonkeyPatch, failure_kind: str,
):
    case = file_case
    owner = case.service.file_operations
    blocked_log = case.root / "blocked-operation-log"
    blocked_log.write_bytes(b"original log obstruction")
    later_directory = case.root / ("later-" + uuid4().hex)
    later_directory.mkdir()
    listed = await case.client.get("/api/v1/workspace/files", params={"path": str(later_directory)})
    assert listed.status_code == 200, listed.text
    later_snapshot = listed.json()["data"]
    entered, release = Event(), Event()
    execute, publish = owner._execute, operations.publish_step
    errors: list[BaseException] = []
    publication_targets: list[Path] = []

    class FailingHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            assert record.exc_info is not None and record.exc_info[1] is not None
            errors.append(record.exc_info[1])
            with (blocked_log / "service.log").open("a", encoding="utf-8"):
                pass

    def blocked(record: operations.FileOperationRecord) -> None:
        entered.set()
        if not release.wait(10):
            raise AssertionError("文件 operation 执行屏障未释放")
        execute(record)

    def fail_publish(stage: Path, target: Path, kind: str) -> None:
        publication_targets.append(target)
        if target.name != "broken.txt":
            publish(stage, target, kind)
        elif failure_kind == "denied":
            raise PermissionError(errno.EACCES, "原文件发布失败", str(target))
        elif failure_kind == "conflict":
            target.write_bytes(b"external replacement")
            publish(stage, target, kind)
        else:
            publish(stage, target, kind)
            target.unlink()
            target.write_bytes(b"external replacement")
            raise OSError(errno.EIO, "原发布后效果待核实", str(target))

    async def enqueue(snapshot: dict, name: str) -> tuple[dict, dict]:
        payload = {"name": name, "kind": "file", "intent": {
            "client_operation_id": "op_" + uuid4().hex, "client_sequence": 1,
            "directory_key": snapshot["directory_key"],
            "expected_directory_revision": snapshot["directory_revision"],
        }}
        response = await case.client.post("/api/v1/workspace/files/entries", params={"path": snapshot["path"]},
                                          json=payload, headers={"X-Request-ID": "req-operation-accept"})
        assert response.status_code == 202, response.text
        assert response.json()["request_id"] == response.headers["X-Request-ID"] == "req-operation-accept"
        return response.json()["data"], payload

    async def wait(operation_id: str) -> dict:
        async with asyncio.timeout(10):
            while True:
                response = await case.client.get("/api/v1/workspace/files/operations", params={
                    "operation_id": operation_id, "expected_workspace_id": case.service.workspace_id,
                }, headers={"X-Request-ID": "req-operation-query"})
                assert response.status_code == 200, response.text
                assert response.json()["request_id"] == response.headers["X-Request-ID"] == "req-operation-query"
                assert str(case.root) not in response.text
                data = response.json()["data"]
                if data["operations"][0]["state"] in {"committed", "rejected"} or data["worker_state"] == "failed":
                    return data
                await asyncio.sleep(0.01)

    handler = FailingHandler(level=logging.ERROR)
    loggers = (operations.logger, failures.logger)
    try:
        with monkeypatch.context() as patch:
            patch.setattr(owner, "_execute", blocked)
            patch.setattr(operations, "publish_step", fail_publish)
            for logger in loggers:
                logger.addHandler(handler)
            accepted, payload = await enqueue(case.listing, "broken.txt")
            assert await asyncio.to_thread(entered.wait, 5)
            successor, _ = await enqueue(later_snapshot, "later.txt")
            release.set()
            status = await wait(accepted["operation_id"])
        receipt = status["operations"][0]
        expected = ("running", "unverified", "reconcile_required") if failure_kind == "unverified" else (
            "rejected", "none", "file_operation_conflict" if failure_kind == "conflict" else "file_operation_failed",
        )
        assert (receipt["state"], receipt["effect"], receipt["error_code"]) == expected
        assert "diagnostic_sink_error=NotADirectoryError" in receipt["error_detail"]
        assert errors and all(any(str(blocked_log) in note for note in error.__notes__) for error in errors)
        assert blocked_log.read_bytes() == b"original log obstruction"
        assert (case.directory / "seed.txt").read_bytes() == b"original\n"
        assert str(case.root) not in json.dumps(file_rows(case), ensure_ascii=False)
        target = case.directory / "broken.txt"
        assert target.exists() == (failure_kind != "denied")
        if target.exists():
            assert target.read_bytes() == b"external replacement"
        if failure_kind == "unverified":
            assert status["worker_state"] == "failed"
            assert "FileEffectUnverifiedError" in status["worker_error"]
            assert "diagnostic_sink_error=NotADirectoryError" in status["worker_error"]
            frozen = owner.store.get(accepted["operation_id"], "local")
            assert frozen.steps[0]["state"] == "publishing" and owner._stage_root(frozen, 0).is_dir()
            assert owner.store.cleanup_pending() == ()
            assert owner.store.get(successor["operation_id"], "local").state == "queued"
            assert not (later_directory / "later.txt").exists()
            with pytest.raises(FileEffectUnverifiedError) as stopped:
                await owner.stop()
            assert stopped.value.__cause__ is errors[0]
            # 原数据库驱动 worker 重启；日志仍失败也必须及时公开拒绝，不能盲重放。
            with pytest.raises(RuntimeError, match="worker 不可用"):
                async with asyncio.timeout(5):
                    await owner.start()
            restarted = await wait(accepted["operation_id"])
            assert restarted["worker_state"] == "failed"
            assert restarted["operations"][0]["effect"] == "unverified"
            assert owner.store.get(successor["operation_id"], "local").state == "queued"
            assert not (later_directory / "later.txt").exists()
            assert target.read_bytes() == b"external replacement"
            with pytest.raises(FileEffectUnverifiedError):
                await owner.stop()
        else:
            assert status["worker_state"] == "running" and status["worker_error"] is None
            assert (await wait(successor["operation_id"]))["operations"][0]["state"] == "committed"
            assert (later_directory / "later.txt").read_bytes() == b""
            replay = await case.client.post("/api/v1/workspace/files/entries", params={"path": case.listing["path"]}, json=payload)
            assert replay.status_code == 202 and replay.json()["data"] == receipt
        assert publication_targets.count(target) == 1
        (case.artifacts / f"operation-log-{failure_kind}.json").write_text(json.dumps({
            "accepted": accepted, "status": status, "errors": [type(error).__name__ for error in errors],
        }, ensure_ascii=False, indent=2) + "\n")
    finally:
        release.set()
        for logger in loggers:
            logger.removeHandler(handler)
        handler.close()
        worker = owner._worker
        if worker is not None and worker.done() and (error := worker.exception()) is not None:
            with pytest.raises(type(error)):
                await owner.stop()


@pytest.mark.parametrize("stage", ["read", "save", "download", "transfer", "text"])
async def test_file_access_log_failure_preserves_original_result_and_retry(
    file_case, monkeypatch: pytest.MonkeyPatch, stage: str,
):
    case = file_case
    target = case.directory / ("new.txt" if stage == "text" else "seed.txt")
    uri = case.listing["path"] + "/" + target.name
    blocked_log = case.root / "blocked-file-access-log"
    blocked_log.write_bytes(b"keep obstruction")
    original = FileNotFoundError(errno.ENOENT, f"private-strerror: {target}", str(target))
    errors: list[BaseException] = []

    class FailingHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            assert record.exc_info is not None and record.exc_info[1] is not None
            errors.append(record.exc_info[1])
            with (blocked_log / "service.log").open("a", encoding="utf-8"):
                pass

    method, endpoint, payload = "GET", "/api/v1/workspace/files/content", None
    if stage == "save":
        current = await case.client.get(endpoint, params={"path": uri})
        assert current.status_code == 200, current.text
        method, payload = "PUT", {"content": "saved\n", "expected_revision": current.json()["data"]["revision"]}
    elif stage in {"download", "transfer"}:
        endpoint = "/api/v1/workspace/files/download"
    elif stage == "text":
        method, endpoint = "POST", "/api/v1/resources/text-mutations"
        payload = {"create": {"target": uri, "content": "created\n", "expected_version": "absent"}}

    async def request():
        return await case.client.request(method, endpoint, params={"path": uri}, json=payload,
                                         headers={"X-Request-ID": "req-file-access-" + stage})

    operation = {"read": Path.open, "download": Path.stat, "text": Path.resolve}.get(stage)

    def fail_path(path: Path, *args, **kwargs):
        if path == target:
            raise original
        assert operation is not None
        return operation(path, *args, **kwargs)

    replace = os.replace

    def fail_replace(source, destination):
        if Path(destination) == target:
            raise original
        return replace(source, destination)

    open_file = anyio.open_file

    async def fail_transfer(path, *args, **kwargs):
        if Path(path) == target:
            raise original
        return await open_file(path, *args, **kwargs)

    before = file_rows(case)
    handler = FailingHandler(level=logging.ERROR)
    loggers = (logging.getLogger(filesystem.__name__), failures.logger)
    try:
        for logger in loggers:
            logger.addHandler(handler)
        with monkeypatch.context() as patch:
            if stage in {"read", "download", "text"}:
                patch.setattr(Path, {"read": "open", "download": "stat", "text": "resolve"}[stage], fail_path)
            elif stage == "save":
                patch.setattr(os, "replace", fail_replace)
            else:
                patch.setattr(anyio, "open_file", fail_transfer)
            failed = await request()
        assert failed.status_code == (200 if stage == "text" else 500 if stage == "transfer" else 404), failed.text
        body = failed.json()
        assert body["request_id"] == failed.headers["X-Request-ID"] == "req-file-access-" + stage
        assert "FileNotFoundError" in failed.text and "diagnostic_sink_error=NotADirectoryError" in failed.text
        assert str(case.root) not in failed.text and "private-strerror" not in failed.text
        if stage == "text":
            receipt = body["data"]["receipt"]
            assert (receipt["status"], receipt["effect"], receipt["error_type"]) == ("error", "not_executed", "FileNotFoundError")
            assert not receipt["journal_id"] and not receipt["files"] and not target.exists()
        assert original in errors and any(str(blocked_log) in note for note in original.__notes__)
        assert file_rows(case) == before
        assert (case.directory / "seed.txt").read_bytes() == b"original\n"
        assert not list(case.directory.glob(".seed.txt.*.tmp"))
        continued = await request()
        assert continued.status_code == 200, continued.text
        assert continued.headers["X-Request-ID"] == "req-file-access-" + stage
        if stage in {"download", "transfer"}:
            assert continued.content == b"original\n"
        elif stage == "text":
            assert continued.json()["data"]["receipt"]["status"] == "success"
            assert target.read_bytes() == b"created\n"
        else:
            assert continued.json()["data"]["content"] == ("saved\n" if stage == "save" else "original\n")
        (case.artifacts / f"file-access-log-{stage}.json").write_text(json.dumps({
            "failure": body, "retry_status": continued.status_code, "errors": [type(error).__name__ for error in errors],
        }, ensure_ascii=False, indent=2) + "\n")
    finally:
        for logger in loggers:
            logger.removeHandler(handler)
        handler.close()
        assert blocked_log.read_bytes() == b"keep obstruction"
