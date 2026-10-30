"""正式资源入口遇到 owner 落盘失败时不发布候选或真实位置。"""

from __future__ import annotations

import errno
import json
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import cast

import pytest

from app.abstractions.workspace_resources import ResourceRequestError
from app.core.mounted_workspaces import MountedWorkspace
from app.services.business.terminal_resources.operations import TerminalOperations
from app.services.infrastructure.browser_manager_client import BrowserManagerClient
from app.services.infrastructure.external_resource_leases import (
    ExternalResourceLeaseLedger,
)
from tests.harness.python.run_context import TestRunContext
from tests.integration.workspace_services.browser.test_browser_owner_revision_drain import (
    browser_client as browser_client,  # noqa: PLC0414
)
from tests.integration.workspace_services.browser.test_browser_owner_revision_drain import (
    owner_mount as owner_mount,  # noqa: PLC0414
)
from tests.integration.workspace_services.test_resource_release import (
    resource_api as resource_api,  # noqa: PLC0414
)
from tests.support.workspace_tool_runtime import workspace_tool_runtime
from tests.support.workspaces import render_test_workspace_config


@pytest.fixture
def resource_workspace(
    browser_client: tuple[BrowserManagerClient, Path, TestRunContext],
) -> tuple[BrowserManagerClient, Path, TestRunContext]:
    _browser, root, _run = browser_client
    # 此测试不调用模型，仅覆盖资源入口；隔离配置不能依赖个人 Provider 凭据。
    config_path = root / ".boxteam/workspace.jsonc"
    config = json.loads(render_test_workspace_config(config_path.read_text(encoding="utf-8")))
    for provider in config["llm"]["providers"]:
        provider["api_key"] = "resource-record-unused-key"
        provider["endpoint"] = "http://127.0.0.1:1/v1"
        provider.pop("auth", None)
    config_path.write_text(json.dumps(config, ensure_ascii=False) + "\n", encoding="utf-8")
    return browser_client


@pytest.mark.parametrize("kind", ["terminal", "browser"])
@pytest.mark.parametrize("log_failure", [False, True], ids=["log-ok", "log-failed"])
async def test_resource_creation_disk_failure_keeps_old_owner_and_allows_explicit_retry(
    resource_workspace: tuple[BrowserManagerClient, Path, TestRunContext],
    owner_mount: MountedWorkspace, monkeypatch: pytest.MonkeyPatch, resource_api, kind: str, caplog: pytest.LogCaptureFixture,
    log_failure: bool,
) -> None:
    _browser, root, run = resource_workspace
    async with workspace_tool_runtime(root, startup_mount=owner_mount) as (container, sid, _context):
        owner = container.terminal_resource_owner if kind == "terminal" else container.browser_resource_owner
        async with resource_api(container, kind) as client:
            endpoint = f"/api/v1/resources/{kind}s"
            response = await client.post(endpoint, json={"session_id": sid, "title": "已有真实资源"})
            assert response.status_code == 200, response.text
            existing = response.json()["data"]["resource_id"]
            before = owner._store.path.read_bytes()
            records = owner._store.all()
            ledger_path = owner._leases._state_path
            assert ledger_path is not None
            leases = ledger_path.read_bytes()
            external = await owner._transport.get_resource(existing)
            pending = owner._store.path.with_suffix(".tmp")
            native_error = PermissionError(errno.EACCES, "owner 发布失败", str(pending))
            sink_error = OSError("日志写入失败 /private/owner-log-secret")
            replace = Path.replace
            call_handlers = logging.Logger.callHandlers

            def fail_publication(path: Path, target: Path) -> Path:
                if path == pending:
                    raise native_error
                return replace(path, target)

            def fail_owner_log(logger: logging.Logger, record: logging.LogRecord) -> None:
                call_handlers(logger, record)
                if record.getMessage().startswith("资源 owner "):
                    raise sink_error

            request_id = f"resource-record-{kind}-failure"
            with monkeypatch.context() as patch:
                patch.setattr(Path, "replace", fail_publication)
                if log_failure:
                    patch.setattr(logging.Logger, "callHandlers", fail_owner_log)
                failed = await client.post(endpoint, json={"session_id": sid, "title": "未发布的资源"},
                                           headers={"X-Request-ID": request_id})
            assert failed.status_code == 500, failed.text
            assert failed.headers["X-Request-ID"] == failed.json()["request_id"] == request_id
            assert failed.json()["message"] == "请求处理失败: stage=http_request, error=PermissionError, errno=13"
            assert "stage=write" in caplog.text and f"kind={kind}" in caplog.text
            assert str(root) not in failed.text and str(pending) not in failed.text
            assert "owner-log-secret" not in failed.text
            if log_failure:
                assert any(str(sink_error) in note for note in native_error.__notes__)
                assert "diagnostic_sink_error=OSError" in caplog.text
            assert owner._store.path.read_bytes() == before and owner._store.all() == records
            assert ledger_path.read_bytes() == leases
            assert [item[f"{kind}_id"] for item in await owner._transport.list_resources()] == [existing]
            identity = "os_pid" if kind == "terminal" else "active_page_id"
            assert (await owner._transport.get_resource(existing))[identity] == external[identity]
            retried = await client.post(endpoint, json={"session_id": sid, "title": "显式重试"})
            assert retried.status_code == 200, retried.text
            created = retried.json()["data"]["resource_id"]
            assert created != existing and set(owner._store.ids()) == {existing, created}
            assert str(root) not in owner._store.path.read_text()
            (run.artifacts_dir / f"record-errors-{kind}-log-{log_failure}.json").write_text(json.dumps({
                "failure": failed.json(), "retry": retried.json(), "existing_resource_id": existing,
                "scope": "正式Session和ASGI资源API、真实Node/PTY或Chromium/HTTP；未验证Gateway/Web或Backend进程重启",
            }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


@pytest.mark.parametrize("kind", ["terminal", "browser"])
async def test_grant_ledger_failure_preserves_original_occupation_and_does_not_dispatch(
    resource_workspace: tuple[BrowserManagerClient, Path, TestRunContext],
    owner_mount: MountedWorkspace, monkeypatch: pytest.MonkeyPatch, resource_api, kind: str, caplog: pytest.LogCaptureFixture,
) -> None:
    _browser, root, run = resource_workspace
    async with workspace_tool_runtime(root, startup_mount=owner_mount) as (container, sid, _context):
        owner = container.terminal_resource_owner if kind == "terminal" else container.browser_resource_owner
        async with resource_api(container, kind) as client:
            response = await client.post(f"/api/v1/resources/{kind}s", json={"session_id": sid})
            assert response.status_code == 200, response.text
            rid = response.json()["data"]["resource_id"]
            async with owner.operation(session_id=sid, thread_id=None,
                                       holder_id="original-holder", operation_id="original") as operation:
                if isinstance(operation, TerminalOperations):
                    await operation.read_terminal(rid)
                else:
                    await operation.perform(rid, "read", {})
                active, = owner._leases.leases_for_resource(rid)
                ledger_path = owner._leases._state_path
                assert ledger_path is not None
                records, ledger = owner._store.path.read_bytes(), ledger_path.read_bytes()
                external = await owner._transport.get_resource(rid)
                pending = ledger_path.with_suffix(".tmp")
                native = PermissionError(errno.EACCES, "lease 发布失败", str(pending))
                replace = Path.replace
                issue = owner._transport.issue_transport_grant
                issued: list[str] = []

                def fail_publication(path: Path, target: Path) -> Path:
                    if path == pending:
                        raise native
                    return replace(path, target)

                async def observe_grant(resource_id: str, **kwargs):
                    issued.append(resource_id)
                    return await issue(resource_id, **kwargs)

                request_id = f"lease-failure-{kind}"
                arguments = {"expected_revision": 1, "thread": response.json()["data"]["source_thread"]}
                with monkeypatch.context() as patch:
                    patch.setattr(Path, "replace", fail_publication)
                    patch.setattr(owner._transport, "issue_transport_grant", observe_grant)
                    failed = await client.post(f"/api/v1/resources/{rid}/transport-grant", json=arguments,
                                               headers={"X-Request-ID": request_id})
                assert failed.status_code == 500, failed.text
                assert failed.headers["X-Request-ID"] == failed.json()["request_id"] == request_id
                assert str(root) not in failed.text and str(pending) not in failed.text
                assert failed.json()["message"] == "请求处理失败: stage=http_request, error=PermissionError, errno=13"
                assert "artifact=external_resource_leases" in caplog.text and "stage=write" in caplog.text
                assert not issued and ledger_path.read_bytes() == ledger
                assert owner._store.path.read_bytes() == records
                assert owner._leases.leases_for_resource(rid) == [active]
                assert ExternalResourceLeaseLedger(state_path=ledger_path).get_lease(active.lease_id) == active
                current = await owner._transport.get_resource(rid)
                identity = "os_pid" if kind == "terminal" else "active_page_id"
                assert current[identity] == external[identity] and current["transport_owner_revision"] == 1
                retried = await client.post(f"/api/v1/resources/{rid}/transport-grant", json=arguments,
                                            headers={"X-Request-ID": request_id})
                assert retried.status_code == 200, retried.text
                assert retried.json()["data"]["grant_token"]
                assert owner._leases.leases_for_resource(rid) == [active]
                (run.artifacts_dir / f"lease-errors-{kind}.json").write_text(json.dumps({
                    "failure": failed.json(), "resource_id": rid, "original_lease_id": active.lease_id,
                    "scope": "正式Session和ASGI授权API、真实PTY/Chromium/HTTP；未验证Gateway/Web或Backend进程重启",
                }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            cold = ExternalResourceLeaseLedger(state_path=ledger_path)
            released = cold.get_lease(active.lease_id)
            assert released is not None and released.status == "released"


@pytest.mark.parametrize("kind", ["terminal", "browser"])
@pytest.mark.parametrize("log_failure", [False, True], ids=["log-ok", "log-failed"])
async def test_resource_creation_receipt_failure_preserves_origin_and_stop_reconciliation(
    resource_workspace: tuple[BrowserManagerClient, Path, TestRunContext],
    owner_mount: MountedWorkspace, monkeypatch: pytest.MonkeyPatch, resource_api,
    kind: str, caplog: pytest.LogCaptureFixture, log_failure: bool,
) -> None:
    _browser, root, run = resource_workspace
    async with workspace_tool_runtime(root, startup_mount=owner_mount) as (container, sid, _context):
        owner = container.terminal_resource_owner if kind == "terminal" else container.browser_resource_owner
        async with resource_api(container, kind) as client:
            create = cast(Callable[..., Awaitable[dict[str, object]]], getattr(owner._transport, f"create_{kind}"))
            original_error = ResourceRequestError("创建响应无法核实 /private/create-receipt-original")
            sink_error = OSError("日志输出失败 /private/create-receipt-log")
            call_handlers = logging.Logger.callHandlers
            snapshots: list[dict[str, object]] = []

            async def corrupt_creation_receipt(**options: object) -> dict[str, object]:
                # 先经真实Node完成创建，再注入损坏的失败回执；不替产品准备记录或lease。
                snapshot = await create(**options)
                snapshots.append(snapshot)
                original_error.creation_receipt = {
                    "resource_id": snapshot[f"{kind}_id"], "workspace_id": owner.workspace_id,
                    "owner_revision": snapshot["transport_owner_revision"],
                    "outcome": "failed", "resource": snapshot,
                }
                raise original_error

            def fail_receipt_log(logger: logging.Logger, record: logging.LogRecord) -> None:
                call_handlers(logger, record)
                if record.getMessage().startswith("外部资源创建失败回执"):
                    raise sink_error

            request_id = f"creation-receipt-{kind}"
            with monkeypatch.context() as patch:
                patch.setattr(owner._transport, f"create_{kind}", corrupt_creation_receipt)
                if log_failure:
                    patch.setattr(logging.Logger, "callHandlers", fail_receipt_log)
                failed = await client.post(f"/api/v1/resources/{kind}s", json={
                    "session_id": sid, "title": "真实创建后的损坏回执",
                }, headers={"X-Request-ID": request_id})
            assert failed.status_code == 500, failed.text
            assert failed.headers["X-Request-ID"] == failed.json()["request_id"] == request_id
            assert failed.json()["message"] == "请求处理失败: stage=http_request, error=ResourceRequestError"
            assert str(root) not in failed.text and "/private/" not in failed.text
            assert any(record.exc_info is not None and record.exc_info[1] is original_error
                       and record.getMessage().startswith("请求处理失败") for record in caplog.records)
            if log_failure:
                assert any(str(sink_error) in note for note in original_error.__notes__)
            snapshot, = snapshots
            rid = str(snapshot[f"{kind}_id"])
            record = owner._store.get(rid)
            assert record is not None
            assert (record.status, record.reconciliation_status, record.owner_revision) == ("creating", "unknown", 1)
            assert record.thread_associations == [record.source_thread]
            pending, = owner._leases.leases_for_resource(rid)
            assert pending.status == "reconcile_required" and pending.operation_id == request_id
            assert pending.turn_stream_id == f"request:{request_id}" and pending.resource_id == rid
            assert pending.holder_session_id == sid and pending.holder_thread_id == record.source_thread.thread_id
            ledger_path = root / ".boxteam/resources.json"
            assert ExternalResourceLeaseLedger(state_path=ledger_path).get_lease(pending.lease_id) == pending
            assert "/private/" not in owner._store.path.read_text()
            found = await client.get(f"/api/v1/resources/{rid}")
            assert found.status_code == 200, found.text
            current = found.json()["data"]
            assert current["resource_id"] == rid and current["reconciliation_status"] == "unknown"
            assert current["owner_revision"] == 1 and current["available_actions"] == ["stop"]
            assert owner._leases.leases_for_resource(rid) == [pending]
            native = await owner._transport.get_resource(rid)
            identity_key = "os_pid" if kind == "terminal" else "active_page_id"
            assert native[identity_key] == snapshot[identity_key] and native["status"] == "running"
            stopped = await client.post(f"/api/v1/resources/{rid}/stop", json={"expected_revision": 1},
                                        headers={"X-Request-ID": f"{request_id}-stop"})
            assert stopped.status_code == 200, stopped.text
            final = stopped.json()["data"]
            assert final["status"] == ("terminated" if kind == "terminal" else "closed")
            assert final["reconciliation_status"] == "verified" and final["owner_revision"] == 2
            assert stopped.headers["X-Request-ID"] == stopped.json()["request_id"] == f"{request_id}-stop"
            verified = await owner._transport.get_resource(rid)
            assert verified["status"] == final["status"] and verified["reconciliation_status"] == "verified"
            assert verified["transport_owner_revision"] == 2
            assert not owner._leases.leases_for_resource(rid)
            settled = ExternalResourceLeaseLedger(state_path=ledger_path).get_lease(pending.lease_id)
            assert settled is not None and settled.status == "settled"
            assert [item[f"{kind}_id"] for item in await owner._transport.list_resources()] == [rid]
            (run.artifacts_dir / f"receipt-diagnostics-{kind}-{log_failure}.json").write_text(json.dumps({
                "failure": failed.json(), "resource_id": rid, "original_lease_id": pending.lease_id,
                "external_identity": snapshot[identity_key], "stopped": final,
                "scope": "正式Session/ASGI API和真实PTY/Chromium/HTTP；创建后注入损坏回执与日志故障；非Gateway/Web或进程重启",
            }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
