from __future__ import annotations

import json
import os
import re
import shutil
import signal
import sqlite3
import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from urllib.request import Request, urlopen

import commentjson
import httpx

from app.core.logging_contract import load_runtime_log_contract
from tests.integration.gateway.runtime_logging_controls import (
    verify_runtime_logging_controls,
)
from tests.support.model_stream.config import prepare_e2e_model_stream_config
from tests.support.ports import integration_port_block_for_file


def _get_json(
    url: str,
    token: str,
    *,
    cookie: str | None = None,
) -> dict[str, object]:
    headers = {"X-Local-Token": token}
    if cookie is not None:
        headers["Cookie"] = cookie
    request = Request(url, headers=headers)
    with urlopen(request, timeout=2) as response:
        payload = json.loads(response.read().decode("utf-8"))
        if not isinstance(payload, dict):
            raise TypeError(f"Gateway 响应必须是对象: url={url}")
        request_id = payload.get("request_id")
        if response.headers.get("X-Request-ID") != request_id:
            raise AssertionError(f"Gateway request_id 头体不一致: payload={payload}")
        return payload


def _request_json(
    url: str,
    token: str,
    *,
    method: str,
    payload: dict[str, object] | None = None,
    cookie: str | None = None,
) -> dict[str, object]:
    body = None
    headers = {"X-Local-Token": token}
    if cookie is not None:
        headers["Cookie"] = cookie
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = Request(url, data=body, method=method, headers=headers)
    with urlopen(request, timeout=10) as response:
        result = json.loads(response.read().decode("utf-8"))
        if not isinstance(result, dict):
            raise TypeError(f"Gateway 响应必须是对象: url={url}")
        request_id = result.get("request_id")
        if response.headers.get("X-Request-ID") != request_id:
            raise AssertionError(f"Gateway request_id 头体不一致: payload={result}")
        return result


def _wait_for_json(
    url: str,
    token: str,
    process: subprocess.Popen[str],
    predicate: Callable[[dict[str, object]], bool],
    *,
    timeout_seconds: float = 120,
    cookie: str | None = None,
) -> dict[str, object]:
    deadline = time.monotonic() + timeout_seconds
    last_error: Exception | None = None
    last_payload: dict[str, object] | None = None
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(
                f"Launcher 提前退出: pid={process.pid}, returncode={process.returncode}"
            )
        try:
            payload = _get_json(url, token, cookie=cookie)
            last_payload = payload
            if predicate(payload):
                return payload
        except (OSError, TypeError, ValueError, AssertionError) as error:
            last_error = error
        time.sleep(0.5)
    raise TimeoutError(
        f"Gateway 响应在 {timeout_seconds} 秒内未满足条件: "
        f"url={url}, last_payload={last_payload}, last_error={last_error}"
    )


def test_launcher_rejects_shared_log_sink_before_rotation(tmp_path: Path) -> None:
    project_root = Path.cwd()
    contract = load_runtime_log_contract(project_root)
    boxteam_home = tmp_path / "home"
    log_path = contract.path_for(contract.root_for(boxteam_home), "gateway")
    log_path.parent.mkdir(parents=True)
    with log_path.open("wb") as stream:
        stream.truncate(contract.max_bytes)
    previous_identity = log_path.stat().st_ino
    result = subprocess.run(
        ["node", "packages/launcher/bin/boxteam.mjs", "start", "--no-open"],
        cwd=project_root,
        env={
            **os.environ,
            "BOXTEAM_HOME": str(boxteam_home),
            "BOXTEAM_RUNTIME_MANIFEST": str(project_root / "out/development-runtime/runtime-manifest.json"),
            "BOXTEAM_SERVICE_LOG_PATH": str(log_path),
            "BOXTEAM_SERVICE_LOG_CAPTURED": "0",
        },
        capture_output=True, text=True, timeout=10, check=False,
    )
    assert result.returncode != 0
    assert "Launcher 显式日志路径不能占用 Gateway 日志文件" in result.stderr
    assert log_path.stat().st_ino == previous_identity
    assert log_path.stat().st_size == contract.max_bytes
    assert not log_path.with_name(log_path.name + ".1").exists()


def test_launcher_handoff_keeps_public_listener_and_workspace_runtime(
    request,
    integration_workspace_root_path: str,
) -> None:
    project_root = Path.cwd().resolve()
    workspace_root = Path(integration_workspace_root_path).resolve()
    output_root = workspace_root.parent
    boxteam_home = output_root / "launcher-home"
    if boxteam_home.exists():
        shutil.rmtree(boxteam_home)
    boxteam_home.mkdir(parents=True)
    token = "launcher-handoff-test-token"
    credential_path = (
        boxteam_home / "state" / "gateway" / "credentials" / "local-token"
    )
    credential_path.parent.mkdir(parents=True, exist_ok=True)
    credential_path.write_text(f"{token}\n", encoding="utf-8")
    credential_path.chmod(0o600)

    port = integration_port_block_for_file(Path(request.node.fspath)).port(20)
    node_executable = shutil.which("node")
    bun_executable = shutil.which("bun")
    if node_executable is None or bun_executable is None:
        raise RuntimeError("Launcher 集成测试需要 PATH 中存在 node 和 bun")
    python_executable = project_root / ".venv" / "bin" / "python"
    if not python_executable.is_file():
        raise FileNotFoundError(f"测试 Python 解释器不存在: {python_executable}")
    runtime_manifest = project_root / "out" / "development-runtime" / "runtime-manifest.json"
    if not runtime_manifest.is_file():
        raise FileNotFoundError(f"Launcher runtime manifest 不存在: {runtime_manifest}")

    artifacts = output_root / "artifacts"
    artifacts.mkdir(parents=True, exist_ok=True)
    stdout_path = artifacts / "launcher-handoff.stdout.log"
    stderr_path = artifacts / "launcher-handoff.stderr.log"
    environment = {
        **os.environ,
        "BOXTEAM_HOME": str(boxteam_home),
        "BOXTEAM_PROJECT_ROOT": str(project_root),
        "BOXTEAM_RUNTIME_MANIFEST": str(runtime_manifest),
        "BOXTEAM_PYTHON_BIN": str(python_executable),
        "BOXTEAM_NODE_BIN": node_executable,
        "BOXTEAM_GATEWAY_PORT": str(port),
        "BOXTEAM_DEFAULT_USER_WORKSPACE_ROOT": str(workspace_root),
        "WORKSPACE_ROOT": str(workspace_root),
        "BOXTEAM_DEVELOPMENT_RESTART_RUNNER": bun_executable,
        "BOXTEAM_DEVELOPMENT_RESTART_SCRIPT": (
            str(project_root / "scripts" / "launch" / "dev.mjs")
        ),
        "BOXTEAM_DEVELOPMENT_RESTART_CWD": str(project_root),
        "BOXTEAM_TEST_MODEL_STREAM_CONFIG": str(prepare_e2e_model_stream_config(
            project_root / "configs/tests/model_stream/model_stream_chat_basic.jsonc",
            output_root=output_root,
            mode="replay",
        )),
    }
    process = None
    stdout_file = stdout_path.open("w", encoding="utf-8")
    stderr_file = stderr_path.open("w", encoding="utf-8")
    workspace_config_path = workspace_root / ".boxteam" / "workspace.jsonc"
    workspace_config = commentjson.loads(
        workspace_config_path.read_text(encoding="utf-8")
    )
    primary_provider = next(
        provider
        for provider in workspace_config["llm"]["providers"]
        if provider["id"] == "primary"
    )
    primary_provider.update(
        {
            "endpoint": "https://opencode.ai/zen/v1",
            "model": "big-pickle",
            "api_key": "${LAUNCHER_HANDOFF_MODEL_KEY}",
            "custom_llm_provider": "openai",
        }
    )
    workspace_config["agents"]["default"]["model"] = {
        "primary_provider": "primary",
        "fallback_providers": [],
    }
    workspace_config_path.write_text(
        json.dumps(workspace_config, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    environment["LAUNCHER_HANDOFF_MODEL_KEY"] = "e2e-local-model-key"
    try:
        process = subprocess.Popen(
            [node_executable, "packages/launcher/bin/boxteam.mjs", "start", "--no-open"],
            cwd=project_root,
            env=environment,
            stdout=stdout_file,
            stderr=stderr_file,
            text=True,
        )
        health_url = f"http://127.0.0.1:{port}/api/gateway/health"
        status_url = f"http://127.0.0.1:{port}/api/gateway/config/reload-status"
        gateway_events_url = f"http://127.0.0.1:{port}/api/gateway/config/events"
        initial_health = _wait_for_json(
            health_url,
            token,
            process,
            lambda payload: payload.get("code") == 0,
        )
        initial_data = initial_health.get("data")
        if not isinstance(initial_data, dict):
            raise TypeError(f"Gateway health data 无效: {initial_health}")
        initial_process_id = initial_data.get("process_id")
        if not isinstance(initial_process_id, int):
            raise TypeError(f"Gateway health 缺少 process_id: {initial_health}")

        guest_request = Request(
            f"http://127.0.0.1:{port}/api/gateway/users/guest",
            data=b'{"tracking":{"test":"launcher-handoff-cookie"}}',
            method="POST",
            headers={
                "X-Local-Token": token,
                "Content-Type": "application/json",
            },
        )
        with urlopen(guest_request, timeout=10) as response:
            guest_payload = json.loads(response.read().decode("utf-8"))
            guest_cookie_header = response.headers.get("Set-Cookie")
        if not isinstance(guest_payload, dict):
            raise TypeError("Gateway 游客登录响应必须是对象")
        if guest_cookie_header is None:
            raise AssertionError("Gateway 游客登录没有返回访问 Cookie")
        guest_cookie = guest_cookie_header.split(";", 1)[0]

        workspace_config_url = f"http://127.0.0.1:{port}/api/v1/config"
        workspace_status_url = (
            f"http://127.0.0.1:{port}/api/v1/config/reload-status"
        )
        workspace_sources_url = f"http://127.0.0.1:{port}/api/v1/config/sources"
        workspace_events_url = f"http://127.0.0.1:{port}/api/v1/config/events"
        initial_config = _get_json(
            workspace_config_url,
            token,
            cookie=guest_cookie,
        )
        initial_config_data = initial_config.get("data")
        if not isinstance(initial_config_data, dict):
            raise TypeError(f"初始 Workspace 配置 data 无效: {initial_config}")
        initial_metadata = initial_config_data.get("metadata")
        if not isinstance(initial_metadata, dict):
            raise TypeError(f"初始 Workspace 配置 metadata 无效: {initial_config}")
        initial_reload = initial_metadata.get("reload")
        if not isinstance(initial_reload, dict):
            raise TypeError(f"初始 Workspace 配置 reload 无效: {initial_config}")
        initial_sources = _get_json(
            workspace_sources_url,
            token,
            cookie=guest_cookie,
        ).get("data")
        if not isinstance(initial_sources, dict):
            raise TypeError("初始 Workspace 配置来源 data 无效")
        runtime_source = next(
            (
                source
                for source in initial_sources.get("sources", [])
                if isinstance(source, dict)
                and source.get("source_key") == "workspace_runtime_override"
            ),
            None,
        )
        if runtime_source is not None and not isinstance(runtime_source, dict):
            raise TypeError("runtime override source 摘要无效")
        api_config = _request_json(
            workspace_config_url,
            token,
            method="PATCH",
            payload={
                "config_layer": "runtime_override",
                "scope": "workspace",
                "base_layer_revision": (
                    runtime_source.get("layer_revision")
                    if runtime_source is not None
                    else None
                ),
                "base_layer_digest": (
                    runtime_source.get("layer_digest")
                    if runtime_source is not None
                    else None
                ),
                "expected_active_revision": initial_reload.get("active_revision"),
                "expected_active_digest": initial_metadata.get("revision"),
                "idempotency_key": "launcher-workspace-config-update",
                "auto_summarize": False,
            },
            cookie=guest_cookie,
        )
        api_config_data = api_config.get("data")
        assert isinstance(api_config_data, dict)
        assert api_config_data["auto_summarize"] is False
        workspace_status = _wait_for_json(
            workspace_status_url,
            token,
            process,
            lambda payload: (
                isinstance(payload.get("data"), dict)
                and payload["data"].get("state") == "active"
            ),
            cookie=guest_cookie,
        )
        workspace_status_data = workspace_status["data"]
        assert isinstance(workspace_status_data, dict)
        workspace_event_payload = _wait_for_json(
            workspace_events_url,
            token,
            process,
            lambda payload: (
                isinstance(payload.get("data"), dict)
                and any(
                    isinstance(event, dict)
                    and event.get("config_domain") == "workspace"
                    and event.get("source") == "workspace-config-service"
                    and "/ui/auto_summarize" in event.get("changed_paths", [])
                    for event in payload["data"].get("events", [])
                )
            ),
            cookie=guest_cookie,
        )
        workspace_event_data = workspace_event_payload["data"]
        assert isinstance(workspace_event_data, dict)
        workspace_events = workspace_event_data["events"]
        assert isinstance(workspace_events, list)
        api_event_ids = {
            event["event_id"]
            for event in workspace_events
            if isinstance(event, dict)
            and event.get("source") == "workspace-config-service"
        }
        assert api_event_ids
        workspace_cursor = workspace_event_data["cursor"]
        assert isinstance(workspace_cursor, int)
        replayed_workspace_events = _get_json(
            workspace_events_url,
            token,
            cookie=guest_cookie,
        )
        replayed_data = replayed_workspace_events["data"]
        assert isinstance(replayed_data, dict)
        assert {
            event["event_id"]
            for event in replayed_data["events"]
            if isinstance(event, dict)
            and event.get("event_id") in api_event_ids
        } == api_event_ids
        after_workspace_cursor = _get_json(
            f"{workspace_events_url}?after={workspace_cursor}",
            token,
            cookie=guest_cookie,
        )
        after_workspace_data = after_workspace_cursor["data"]
        assert isinstance(after_workspace_data, dict)
        assert after_workspace_data["events"] == []

        workspace_config_path = workspace_root / ".boxteam" / "workspace.jsonc"
        workspace_document = workspace_config_path.read_text(encoding="utf-8")
        stripped_workspace_document = workspace_document.rstrip()
        if not stripped_workspace_document.endswith("}"):
            raise AssertionError("Workspace JSONC 根文档不是对象")
        workspace_config_path.write_text(
            stripped_workspace_document[:-1]
            + ',\n  "ui": {"default_model": "integration-file-model"}\n}\n',
            encoding="utf-8",
        )
        file_config = _wait_for_json(
            workspace_config_url,
            token,
            process,
            lambda payload: (
                isinstance(payload.get("data"), dict)
                and payload["data"].get("default_model") == "integration-file-model"
            ),
            cookie=guest_cookie,
        )
        file_config_data = file_config["data"]
        assert isinstance(file_config_data, dict)
        assert file_config_data["default_model"] == "integration-file-model"
        file_event_payload = _wait_for_json(
            f"{workspace_events_url}?after={workspace_cursor}",
            token,
            process,
            lambda payload: (
                isinstance(payload.get("data"), dict)
                and any(
                    isinstance(event, dict)
                    and event.get("config_domain") == "workspace"
                    and "/ui/default_model" in event.get("changed_paths", [])
                    for event in payload["data"].get("events", [])
                )
            ),
            cookie=guest_cookie,
        )
        file_event_data = file_event_payload["data"]
        assert isinstance(file_event_data, dict)
        assert any(
            isinstance(event, dict)
            and event.get("config_domain") == "workspace"
            and "/ui/default_model" in event.get("changed_paths", [])
            for event in file_event_data["events"]
        )

        session_payload = _request_json(
            f"http://127.0.0.1:{port}/api/v1/sessions",
            token,
            method="POST",
            payload={"title": "Configuration handoff session"},
            cookie=guest_cookie,
        )
        session_data = session_payload.get("data")
        assert isinstance(session_data, dict)
        session_id = session_data.get("session_id")
        assert isinstance(session_id, str)
        thread_id = session_data.get("thread_id")
        assert isinstance(thread_id, str)
        message_payload = _request_json(
            f"http://127.0.0.1:{port}/api/v1/sessions/{session_id}/messages",
            token,
            method="POST",
            payload={
                "message": {"content": "请回复 LAUNCHER_HANDOFF_JOB_OK"},
                "run": {"mode": "single_agent", "agent_id": "default"},
            },
            cookie=guest_cookie,
        )
        message_data = message_payload.get("data")
        assert isinstance(message_data, dict)
        assert message_data["thread_id"] == thread_id
        job_id = message_data.get("job_id")
        assert isinstance(job_id, str)
        completed_job = _wait_for_json(
            f"http://127.0.0.1:{port}/api/v1/jobs/{job_id}",
            token,
            process,
            lambda payload: (
                isinstance(payload.get("data"), dict)
                and payload["data"].get("status") in {"completed", "succeeded"}
            ),
            timeout_seconds=60,
            cookie=guest_cookie,
        )
        completed_job_data = completed_job["data"]
        assert isinstance(completed_job_data, dict)
        assert completed_job_data["status"] in {"completed", "succeeded"}
        assert completed_job_data["thread_id"] == thread_id
        messages = _get_json(
            f"http://127.0.0.1:{port}/api/v1/sessions/{session_id}/messages",
            token,
            cookie=guest_cookie,
        )["data"]
        assert isinstance(messages, dict)
        assert [
            (message["session_id"], message["thread_id"], message["content"])
            for message in messages["items"]
            if message["role"] == "assistant"
        ] == [(session_id, thread_id, "基础回放")]

        config_path = boxteam_home / "config" / "gateway.jsonc"
        document = config_path.read_text(encoding="utf-8")
        poll_match = re.search(
            r'("poll_interval_seconds":\s*)([0-9]+(?:\.[0-9]+)?)'
            r'(\s*// Gateway 进程健康检查轮询周期)',
            document,
        )
        if poll_match is None:
            raise AssertionError("测试没有找到 Gateway runtime 配置字段")
        previous_poll_interval = float(poll_match.group(2))
        next_poll_interval = previous_poll_interval + 0.25
        changed_document = (
            document[: poll_match.start(2)]
            + f"{next_poll_interval:g}"
            + document[poll_match.end(2) :]
        )
        config_path.write_text(changed_document, encoding="utf-8")
        pending = _wait_for_json(
            status_url,
            token,
            process,
            lambda payload: isinstance(payload.get("data"), dict)
            and payload["data"].get("state") == "pending_restart",
        )
        pending_data = pending["data"]
        assert isinstance(pending_data, dict)
        candidate_id = pending_data.get("candidate_id")
        assert isinstance(candidate_id, str)
        candidate_ref = pending_data.get("candidate_ref")
        assert isinstance(candidate_ref, str)
        gateway_event_payload = _wait_for_json(
            gateway_events_url,
            token,
            process,
            lambda payload: (
                isinstance(payload.get("data"), dict)
                and any(
                    isinstance(event, dict)
                    and event.get("candidate_id") == candidate_id
                    for event in payload["data"].get("events", [])
                )
            ),
        )
        gateway_event_data = gateway_event_payload["data"]
        assert isinstance(gateway_event_data, dict)
        gateway_events = gateway_event_data["events"]
        assert isinstance(gateway_events, list)
        gateway_candidate_event_ids = {
            event["event_id"]
            for event in gateway_events
            if isinstance(event, dict) and event.get("candidate_id") == candidate_id
        }
        assert gateway_candidate_event_ids
        gateway_event_cursor = gateway_event_data["cursor"]
        assert isinstance(gateway_event_cursor, int)

        log_contract = load_runtime_log_contract(project_root)
        log_root = log_contract.root_for(boxteam_home)
        gateway_log = log_contract.path_for(log_root, "gateway")
        launcher_log = log_contract.path_for(log_root, "launcher")
        gateway_log_identity = gateway_log.stat().st_ino
        assert launcher_log.is_file()
        logging_states = verify_runtime_logging_controls(
            f"http://127.0.0.1:{port}", token, boxteam_home,
            launcher_pid=process.pid, artifacts=artifacts,
        )

        restart_request = Request(
            f"http://127.0.0.1:{port}/api/gateway/runtime/restart-development",
            method="POST",
            headers={"X-Local-Token": token},
        )
        with urlopen(restart_request, timeout=10) as response:
            restart_payload = json.loads(response.read().decode("utf-8"))
        assert restart_payload["data"]["previous_process_id"] == initial_process_id

        final_health = _wait_for_json(
            health_url,
            token,
            process,
            lambda payload: (
                isinstance(payload.get("data"), dict)
                and payload["data"].get("process_id") != initial_process_id
            ),
        )
        final_data = final_health["data"]
        assert isinstance(final_data, dict)
        new_process_id = final_data["process_id"]
        assert isinstance(new_process_id, int)
        final_status = _wait_for_json(
            status_url,
            token,
            process,
            lambda payload: isinstance(payload.get("data"), dict)
            and payload["data"].get("state") == "active"
            and payload["data"].get("candidate_ref") is None,
        )
        final_status_data = final_status["data"]
        assert isinstance(final_status_data, dict)
        assert final_status_data["restart_required"] is False
        assert final_status_data["active_revision"] == 3
        assert candidate_ref not in {final_status_data.get("candidate_ref")}
        replayed_gateway_events = _get_json(gateway_events_url, token)
        replayed_gateway_data = replayed_gateway_events["data"]
        assert isinstance(replayed_gateway_data, dict)
        assert {
            event["event_id"]
            for event in replayed_gateway_data["events"]
            if isinstance(event, dict)
            and event.get("event_id") in gateway_candidate_event_ids
        } == gateway_candidate_event_ids
        promoted_gateway_events = _get_json(
            f"{gateway_events_url}?after={gateway_event_cursor}",
            token,
        )
        promoted_gateway_data = promoted_gateway_events["data"]
        assert isinstance(promoted_gateway_data, dict)
        assert any(
            isinstance(event, dict)
            and event.get("candidate_id") == candidate_id
            and event.get("result") == "applied"
            for event in promoted_gateway_data["events"]
        )

        database = sqlite3.connect(
            boxteam_home / "state" / "gateway" / "gateway.sqlite"
        )
        try:
            snapshot = database.execute(
                "SELECT active_revision, promoted_generation "
                "FROM config_active_snapshot WHERE config_domain = 'gateway'"
            ).fetchone()
            pending_record = database.execute(
                "SELECT state FROM config_pending_candidate "
                "WHERE config_domain = 'gateway' AND candidate_id = ?",
                (candidate_id,),
            ).fetchone()
            assert pending_record == ("active",)
            intent_record = database.execute(
                "SELECT state, target_generation FROM gateway_restart_intent "
                "WHERE candidate_id = ?",
                (candidate_id,),
            ).fetchone()
            assert intent_record is not None
            assert intent_record[0] == "active"
            assert snapshot == (3, intent_record[1])
            registry_metadata_record = database.execute(
                "SELECT payload_json FROM gateway_config "
                "WHERE config_key = 'workspace_registry_meta'"
            ).fetchone()
            assert registry_metadata_record is not None
            registry_metadata = json.loads(str(registry_metadata_record[0]))
            assert registry_metadata["runtime_generation"] == intent_record[1]
            claim_count = database.execute(
                "SELECT COUNT(*) FROM config_apply_claim "
                "WHERE config_domain = 'gateway'"
            ).fetchone()
            assert claim_count == (0,)
            journal = database.execute(
                "SELECT state FROM config_apply_journal "
                "WHERE config_domain = 'gateway' AND candidate_id = ? "
                "ORDER BY updated_at DESC LIMIT 1",
                (candidate_id,),
            ).fetchone()
            assert journal == ("committed",)
            generations = database.execute(
                "SELECT loaded_source, state, listener_state "
                "FROM gateway_runtime_generation"
            ).fetchall()
            assert ("pending", "active", "serving") in generations
        finally:
            database.close()

        time.sleep(1)
        stdout_file.flush()
        stderr_file.flush()
        log_text = (
            stdout_path.read_text(encoding="utf-8")
            + stderr_path.read_text(encoding="utf-8")
        )
        assert f"Started server process [{initial_process_id}]" in log_text
        assert f"Started server process [{new_process_id}]" in log_text
        assert f"Finished server process [{initial_process_id}]" in log_text
        assert log_text.count("启动工作区后端:") == 1
        persisted_gateway_log = gateway_log.read_text(encoding="utf-8")
        assert persisted_gateway_log.count(f"Started server process [{initial_process_id}]") == 1
        assert persisted_gateway_log.count(f"Started server process [{new_process_id}]") == 1
        assert persisted_gateway_log.count("启动工作区后端:") == 1
        persisted_launcher_log = launcher_log.read_text(encoding="utf-8")
        assert "启动工作区后端:" not in persisted_launcher_log
        assert f"service=launcher pid={process.pid} runtime_instance_id=" in persisted_launcher_log
        gateway_instance_ids = []
        for gateway_pid in (initial_process_id, new_process_id):
            identities = set(re.findall(
                rf"service=gateway pid={gateway_pid} runtime_instance_id=([0-9a-f]+)",
                persisted_gateway_log,
            ))
            assert len(identities) == 1, (gateway_pid, identities)
            assert re.search(
                rf"service=gateway pid={gateway_pid} runtime_instance_id=[0-9a-f]+ logger=uvicorn.error Started server process \[{gateway_pid}\]",
                persisted_gateway_log,
            )
            gateway_instance_ids.append(identities.pop())
        assert gateway_instance_ids[0] != gateway_instance_ids[1]
        with httpx.Client(timeout=5, trust_env=False) as client:
            control_url = f"http://127.0.0.1:{port}/api/gateway/internal/runtime-logging"
            headers = {"X-Local-Token": token}
            current = client.get(control_url, headers=headers).json()["data"]
            assert current["pid"] == new_process_id
            assert current["runtime_instance_id"] != logging_states["gateway"]["runtime_instance_id"]
            stale = {"runtime_instance_id": logging_states["gateway"]["runtime_instance_id"],
                         "expected_revision": None, "revision": "stale-generation", "level": "critical", "pretty": False}
            assert client.post(control_url, headers=headers, json=stale).status_code == 409
            assert client.get(control_url, headers=headers).json()["data"] == current
        assert "logger=uvicorn.error" in persisted_gateway_log
        assert gateway_log.stat().st_ino == gateway_log_identity
        assert not gateway_log.with_name(gateway_log.name + ".1").exists()
    finally:
        if process is not None and process.poll() is None:
            process.send_signal(signal.SIGTERM)
            try:
                process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)
        stdout_file.close()
        stderr_file.close()
