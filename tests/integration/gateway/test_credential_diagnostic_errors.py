"""真实 Gateway 鉴权在原生文件与日志双重故障后保留原拒绝并恢复。"""

from __future__ import annotations

import json
import logging
import os
from contextlib import ExitStack
from datetime import datetime
from pathlib import Path
from traceback import format_exception

import httpx
import pytest
from fastapi import FastAPI

from app.gateway.credentials import FederationCredentialStore
from tests.harness.python.run_context import TestRunContext
from tests.support.ports import integration_port_block_for_file
from tests.support.processes.gateway import (
    close_gateway_process,
    reset_gateway_persistent_state,
    start_gateway_process,
)


def create_credential_diagnostic_failure_app() -> FastAPI:
    """只注入可开关的日志 I/O 故障，路由、认证和业务 owner 使用原应用。"""
    from app.gateway.main import app

    control = Path(os.environ["BOXTEAM_TEST_CREDENTIAL_SINK_CONTROL"])
    destination = Path(os.environ["BOXTEAM_TEST_CREDENTIAL_SINK_DESTINATION"])
    audit = Path(os.environ["BOXTEAM_TEST_CREDENTIAL_SINK_AUDIT"])

    class FailingLogHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            if not control.is_file():
                return
            primary = record.exc_info[1] if record.exc_info is not None else None
            with audit.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps({
                    "message": record.getMessage(),
                    "error_type": type(primary).__name__ if primary is not None else None,
                    "diagnostic": "".join(format_exception(primary)) if primary is not None else None,
                }, ensure_ascii=False) + "\n")
            with destination.open("a", encoding="utf-8"):
                pass

    logging.getLogger("app.gateway.credentials").addHandler(FailingLogHandler(logging.ERROR))
    return app


@pytest.mark.skipif(os.name == "nt", reason="使用 POSIX 私有文件权限；不代替 Windows ACL 实机验收")
@pytest.mark.asyncio
async def test_federation_credential_and_log_failures_keep_http_classification_and_recover(
    request: pytest.FixtureRequest, integration_workspace_root_path: str,
) -> None:
    context = TestRunContext.from_test_file(Path(__file__)).prepare()
    workspace = Path(integration_workspace_root_path)
    reset_gateway_persistent_state(workspace_root=workspace)
    runtime = context.runtime_root_for_node(request.node.nodeid)
    runtime.mkdir(parents=True, exist_ok=True)
    blocked = runtime / "blocked-log-directory"
    blocked.write_text("日志路径阻塞文件", encoding="utf-8")
    control = runtime / "sink-enabled"
    audit = context.artifacts_dir / "credential-sink-diagnostics.jsonl"
    ports = integration_port_block_for_file(Path(request.node.fspath))
    evidence = {}
    with ExitStack() as processes:
        gateway = start_gateway_process(
            workspace_root=workspace, default_backend_url=None, port=ports.port(0),
            artifacts_dir=context.artifacts_dir, asgi_app=f"{__name__}:create_credential_diagnostic_failure_app",
            extra_env={"BOXTEAM_TEST_CREDENTIAL_SINK_CONTROL": str(control),
                       "BOXTEAM_TEST_CREDENTIAL_SINK_DESTINATION": str(blocked / "service.log"),
                       "BOXTEAM_TEST_CREDENTIAL_SINK_AUDIT": str(audit)},
        )
        processes.callback(close_gateway_process, gateway)
        path = context.boxteam_home / "state/gateway/credentials/federation.json"
        # 使用原签发入口生成授权；磁盘改动仅制造外部损坏，不代产品执行认证。
        store = FederationCredentialStore(storage_path=path)
        credential = store.issue(connection_id="rgw_diagnostic", peer_gateway_id="gateway_peer")
        original = path.read_bytes()
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{gateway.port}",
            headers={"X-BoxTeam-Federation-Token": credential.token}, timeout=20,
        ) as client:
            endpoint = "/api/gateway/federation/workspaces"
            healthy = await client.get(endpoint)
            assert healthy.status_code == 200, healthy.text
            control.touch()
            for damage, status, error_type, stage in [
                ("permissions", 401, "PermissionError", "permissions"),
                ("json", 500, "JSONDecodeError", "decode"),
            ]:
                if damage == "permissions":
                    path.chmod(0o644)
                else:
                    path.write_bytes(b'{"secret":"' + credential.token.encode() + b'", invalid}')
                damaged = path.read_bytes()
                request_id = f"req_credential_{damage}"
                rejected = await client.get(endpoint, headers={"X-Request-ID": request_id})
                assert rejected.status_code == status, rejected.text
                assert rejected.json()["request_id"] == rejected.headers["X-Request-ID"] == request_id
                assert f"error={error_type}" in rejected.text
                if damage == "permissions":
                    assert f"stage={stage}" in rejected.text and "diagnostic_sink_error=NotADirectoryError" in rejected.text
                assert str(path) not in rejected.text and str(blocked) not in rejected.text
                assert credential.token not in rejected.text and path.read_bytes() == damaged
                evidence[damage] = rejected.json()
                path.write_bytes(original)
                path.chmod(0o600)
                recovered = await client.get(endpoint)
                assert recovered.status_code == 200, recovered.text
                assert recovered.json()["data"] == healthy.json()["data"]
                assert path.read_bytes() == original and store.verify(credential.token) == credential
            control.unlink()
            assert (await client.get(endpoint)).status_code == 200
        records = [json.loads(line) for line in audit.read_text().splitlines()]
        assert len(records) == 2
        assert records[0]["error_type"] == "PermissionError" and records[1]["error_type"] is None
        assert str(path) in records[0]["diagnostic"] and records[1]["diagnostic"] is None
        assert all(f"request_id=req_credential_{name}" in record["message"]
                   for name, record in zip(("permissions", "json"), records, strict=True))
        assert credential.token not in audit.read_text()
        logs = (context.artifacts_dir / "gateway.stderr.log").read_text()
        assert str(blocked / "service.log") in logs and "NotADirectoryError" in logs
        assert credential.token not in logs
        (context.artifacts_dir / "credential-errors.json").write_text(
            json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
        )


@pytest.mark.skipif(os.name == "nt", reason="使用 POSIX 权限故障；不代替 Windows ACL 实机验收")
@pytest.mark.asyncio
async def test_local_token_corruption_and_log_failures_keep_http_errors_and_original_token(
    request: pytest.FixtureRequest, integration_workspace_root_path: str,
) -> None:
    context = TestRunContext.from_test_file(Path(__file__)).prepare()
    workspace = Path(integration_workspace_root_path)
    reset_gateway_persistent_state(workspace_root=workspace)
    runtime = context.runtime_root_for_node(request.node.nodeid)
    runtime.mkdir(parents=True, exist_ok=True)
    blocker = runtime / "blocked-local-token-log"
    blocker.write_text("日志目录阻塞文件", encoding="utf-8")
    control = runtime / "local-token-sink-enabled"
    audit = context.artifacts_dir / "local-token-sink-diagnostics.jsonl"
    audit.unlink(missing_ok=True)
    ports = integration_port_block_for_file(Path(request.node.fspath))
    evidence = []
    with ExitStack() as processes:
        gateway = start_gateway_process(
            workspace_root=workspace, default_backend_url=None, port=ports.port(0),
            artifacts_dir=context.artifacts_dir, asgi_app=f"{__name__}:create_credential_diagnostic_failure_app",
            extra_env={"BOXTEAM_TEST_CREDENTIAL_SINK_CONTROL": str(control),
                       "BOXTEAM_TEST_CREDENTIAL_SINK_DESTINATION": str(blocker / "service.log"),
                       "BOXTEAM_TEST_CREDENTIAL_SINK_AUDIT": str(audit)},
        )
        processes.callback(close_gateway_process, gateway)
        path = context.boxteam_home / "state/gateway/credentials/local-token"
        # 移除启动装具的固定 token，关键凭据由正式产品入口创建。
        path.unlink()
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{gateway.port}", timeout=20) as client:
            issued = await client.get("/api/gateway/auth/local-credential")
            assert issued.status_code == 200, issued.text
            token = issued.json()["data"]["token"]
            assert token and token != "local-dev-token"
            original = path.read_bytes()
            headers = {"X-Local-Token": token}
            healthy = await client.get("/api/gateway/workspaces", headers=headers)
            assert healthy.status_code == 200, healthy.text
            for sink_failed in (False, True):
                if sink_failed:
                    control.touch()
                for damage, error_type in (("empty", "ValueError"), ("utf8", "UnicodeDecodeError"),
                                           ("permissions", "PermissionError"), ("directory", "IsADirectoryError")):
                    if damage == "empty":
                        path.write_bytes(b" \n")
                    elif damage == "utf8":
                        path.write_bytes(token.encode() + b"\xff")
                    elif damage == "permissions":
                        path.chmod(0o644)
                    else:
                        path.unlink()
                        path.mkdir()
                    damaged = path.read_bytes() if damage != "directory" else None
                    try:
                        request_id = f"req_local_credential_{damage}_{int(sink_failed)}"
                        rejected = await client.get("/api/gateway/workspaces", headers={
                            **headers, "X-Request-ID": request_id,
                        })
                        assert rejected.status_code == 500, rejected.text
                        assert rejected.json()["request_id"] == rejected.headers["X-Request-ID"] == request_id
                        assert f"error={error_type}" in rejected.text
                        assert all(value not in rejected.text for value in (str(path), str(blocker), token))
                        if damage == "directory":
                            assert path.is_dir() and not list(path.iterdir())
                        else:
                            assert path.read_bytes() == damaged
                        evidence.append({"damage": damage, "sink_failed": sink_failed, "response": rejected.json()})
                    finally:
                        if damage == "directory":
                            path.rmdir()
                        path.write_bytes(original)
                        path.chmod(0o600)
                    recovered = await client.get("/api/gateway/workspaces", headers=headers)
                    assert recovered.status_code == 200, recovered.text
                    recovered_data = recovered.json()["data"]
                    healthy_data = healthy.json()["data"]
                    # list_dtos 每次生成新的 checked_at；其余字段必须逐项保持。
                    for current, prior in zip(recovered_data["items"], healthy_data["items"], strict=True):
                        assert datetime.fromisoformat(current["checked_at"]) >= datetime.fromisoformat(prior["checked_at"])
                        current["checked_at"] = prior["checked_at"]
                    assert recovered_data == healthy_data
                    reloaded = await client.get("/api/gateway/auth/local-credential")
                    assert reloaded.status_code == 200 and reloaded.json()["data"]["token"] == token
                    assert path.read_bytes() == original
            control.unlink()
        records = [json.loads(line) for line in audit.read_text().splitlines()]
        assert len(records) == 4
        for record, damage in zip(records, ("empty", "utf8", "permissions", "directory"), strict=True):
            assert "artifact=local_token" in record["message"] and str(path) in record["message"]
            assert f"request_id=req_local_credential_{damage}_1" in record["message"]
            if damage in {"empty", "utf8"}:
                assert record["error_type"] is None and record["diagnostic"] is None
            else:
                assert str(path) in record["diagnostic"]
        assert token not in audit.read_text()
        logs = (context.artifacts_dir / "gateway.stderr.log").read_text()
        assert str(blocker / "service.log") in logs and token not in logs
        (context.artifacts_dir / "local-token-errors.json").write_text(
            json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
        )
