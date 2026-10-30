"""真实调试 claim 冷读取的身份拒绝不被诊断日志故障替换。"""

from __future__ import annotations

import errno
import json
import logging
from pathlib import Path

import pytest

from app.services.infrastructure.node_debug.process import process_identity
from app.services.infrastructure.node_debug.process.process_identity import (
    probe_process_identity,
)
from tests.harness.python.run_context import TestRunContext
from tests.integration.backend.agents.test_node_debug_receive_errors import (
    DebugReceiveClient,
)
from tests.integration.backend.agents.test_node_debug_receive_errors import (
    debug_receive_client as debug_receive_client,  # noqa: PLC0414 - 工作区由当前测试路径确定
)


@pytest.mark.skipif(process_identity.sys.platform != "linux", reason="真实 /proc 故障边界只在 Linux 验证")
@pytest.mark.parametrize("failure", ["stat_format", "stat_fields", "empty_boot_id"])
@pytest.mark.parametrize("sink_fails", [False, True], ids=["log-ok", "log-failed"])
async def test_cold_identity_rejection_preserves_claim_process_and_primary_diagnostics(
    debug_receive_client: DebugReceiveClient, failure: str, sink_fails: bool,
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    client, store, owner, root, reopen = debug_receive_client
    configured = await client.post("/api/v1/debug/node/configurations", json={
        **owner, "name": "身份诊断故障后原方案恢复", "script_path": "entry.mjs",
    })
    assert configured.status_code == 200, configured.text
    configuration_id = configured.json()["data"]["active_configuration_id"]
    payload = {**owner, "configuration_id": configuration_id, "path": "entry.mjs"}
    started = await client.post("/api/v1/debug/node/start", json=payload)
    assert started.status_code == 200, started.text
    pid = started.json()["data"]["pid"]
    claim = store.read_launch_claim(**owner)
    manifest_bytes, _manifest = store.read_manifest_payload(**owner)
    assert claim is not None and claim.phase == "running" and claim.pid == pid
    assert manifest_bytes is not None
    await reopen(False)

    target = Path(f"/proc/{pid}/stat") if failure.startswith("stat_") else Path("/proc/sys/kernel/random/boot_id")
    private = str(root / "identity-source-secret")
    raw = private if failure == "stat_format" else f"{pid} ({private}) S 0" if failure == "stat_fields" else "\n"
    read_text = Path.read_text
    capture = caplog.handler.emit
    observed: list[Path] = []
    sink_errors: list[OSError] = []
    blocker = root / "identity-log-secret"
    blocker.write_text("普通文件不能承载日志子文件")
    destination = blocker / "service.log"
    stage = "boot_id_parse" if failure == "empty_boot_id" else "process_stat_parse"

    def read_identity(path: Path, encoding: str | None = None, errors: str | None = None) -> str:
        if path == target:
            observed.append(path)
            return raw
        return read_text(path, encoding=encoding, errors=errors)

    def emit(record: logging.LogRecord) -> None:
        capture(record)
        if sink_fails and (record.name == process_identity.__name__ or f"stage={stage}" in record.getMessage()):
            try:
                destination.write_text(record.getMessage())
            except OSError as error:
                sink_errors.append(error)
                raise

    process_identity._read_linux_boot_id.cache_clear()
    try:
        with monkeypatch.context() as patch, caplog.at_level(logging.ERROR):
            patch.setattr(Path, "read_text", read_identity)
            patch.setattr(caplog.handler, "emit", emit)
            failed = await client.get("/api/v1/debug/node", params=owner)
    finally:
        process_identity._read_linux_boot_id.cache_clear()
    assert failed.status_code == 500, failed.text
    assert failed.json()["request_id"] == failed.headers["X-Request-ID"]
    assert observed == [target]
    assert private not in failed.text and str(target) not in failed.text and str(destination) not in failed.text
    errors = [record.exc_info[1] for record in caplog.records if record.exc_info and "stage=http_request" in record.getMessage()]
    assert len(errors) == 1
    error = errors[0]
    assert isinstance(error, RuntimeError) and f"stage={stage}" in str(error)
    assert f"source={process_identity.IDENTITY_SOURCE_LINUX_PROC}" in str(error)
    assert private not in str(error) and str(destination) not in str(error)
    cause = error.__cause__
    assert isinstance(cause, RuntimeError)
    assert any(str(target) in note and (raw in note or failure == "empty_boot_id") for note in cause.__notes__)
    assert any(record.exc_info and record.exc_info[1] is cause for record in caplog.records)
    if sink_fails:
        assert len(sink_errors) == 1 and sink_errors[0].errno == errno.ENOTDIR
        assert any(str(sink_errors[0]) in note for note in cause.__notes__)
        assert "diagnostic_sink_error=NotADirectoryError" in str(error)
    else:
        assert not sink_errors and "diagnostic_sink_error" not in str(error)
    assert store.read_launch_claim(**owner) == claim
    assert store.read_manifest_payload(**owner)[0] == manifest_bytes
    identity = probe_process_identity(pid)
    assert identity is not None
    assert identity.compare(recorded_source=claim.process_identity_source, recorded_start_marker=claim.process_start_marker) == "match"

    retried = await client.get("/api/v1/debug/node", params=owner)
    assert retried.status_code == 200, retried.text
    state = retried.json()["data"]
    assert state["status"] == "idle" and state["pid"] is None
    assert state["active_configuration_id"] == configuration_id
    assert state["actions"][:-1] == started.json()["data"]["actions"]
    assert len([action for action in state["actions"] if action["action"] == "reconcile_settled"]) == 1
    assert probe_process_identity(pid) is None
    settled = store.read_launch_claim(**owner)
    assert settled is not None and settled.phase == "settled" and settled.process_instance_id == claim.process_instance_id
    context = TestRunContext.from_test_file(Path(__file__)).prepare()
    mode = "log-failed" if sink_fails else "log-ok"
    (context.artifacts_dir / f"{failure}-{mode}.json").write_text(json.dumps({
        "started": started.json(), "failed": failed.json(), "retried": retried.json(),
        "claim": claim.model_dump(mode="json"), "settled": settled.model_dump(mode="json"),
        "scope": "ASGI HTTP、真实Node进程及产品claim的冷service读取；不代Gateway、独立进程重启或Windows",
    }, ensure_ascii=False, indent=2) + "\n")
