"""模型为显式 TCP 回放替身；关闭链路使用真实 Gateway、backend、PTY 与 Chromium。"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import threading
import time
from collections.abc import Iterator
from contextlib import ExitStack
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest

from app.testing.model_stream import get_protocol_codec, load_cassette
from configs.runtime import read_jsonc_object, validate_config
from tests.support.gateway_processes import (
    LOCAL_TOKEN_HEADERS,
    acquire_gateway_guest,
    close_gateway_process,
    reset_gateway_persistent_state,
    start_gateway_process,
)
from tests.support.ports import integration_port_block_for_file


def _active_group_members(groups: set[int]) -> dict[int, tuple[int, str]]:
    """只观察已登记服务组的活进程，保留 PID 实例用于失败时精确回收。"""
    result: dict[int, tuple[int, str]] = {}
    for path in Path("/proc").iterdir():
        if not path.name.isdecimal():
            continue
        try:
            fields = (path / "stat").read_text().rsplit(")", 1)[1].split()
        except FileNotFoundError:
            continue
        group = int(fields[2])
        if group in groups and fields[0] not in {"Z", "X"}:
            pid = int(path.name)
            try:
                assert os.getpgid(pid) == group
            except ProcessLookupError:
                continue
            result[pid] = (group, fields[19])
    return result


@pytest.fixture(scope="module", params=["responses", "chat_completions"])
def model_protocol(request: pytest.FixtureRequest) -> str:
    return str(request.param)


@pytest.fixture(scope="module")
def integration_workspace_root_path(
    integration_workspace_root_path: str, model_protocol: str,
) -> str:
    # 每种协议独立初始化业务状态，避免上个替身的 active 配置快照跨参数复用。
    workspace = Path(integration_workspace_root_path)
    reset_gateway_persistent_state(workspace_root=workspace)
    state = workspace / ".boxteam"
    if state.exists():
        shutil.rmtree(state)
    return str(workspace)


@pytest.fixture
def model_replay(integration_workspace_root_path: str, model_protocol: str) -> Iterator[tuple[str, list[dict[str, object]]]]:
    requests: list[dict[str, object]] = []
    protocol_directory = "openai_responses" if model_protocol == "responses" else "openai_chat"
    fixture = Path.cwd() / "tests/fixtures/model_stream/handwritten" / protocol_directory / "basic-text.json"
    response = load_cassette(fixture).interactions[0]
    codec = get_protocol_codec(response.protocol)
    codec.validate_stream(response.response.frames, label="关闭验收模型响应")
    body = b"".join(codec.encode(frame) for frame in response.response.frames)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, _format: str, *args: object) -> None:
            pass

        def do_POST(self) -> None:
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(payload)
            expected_path = "/v1/responses" if model_protocol == "responses" else "/v1/chat/completions"
            assert self.path == expected_path
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    with ExitStack() as lifetime:
        def save_replay_requests() -> None:
            output = Path(integration_workspace_root_path).parent / "artifacts"
            output.mkdir(exist_ok=True)
            (output / "model-replay-requests.json").write_text(json.dumps(requests, ensure_ascii=False, indent=2))

        lifetime.callback(save_replay_requests)
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        lifetime.callback(server.server_close)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def join_server_thread() -> None:
            thread.join(timeout=5)
            assert not thread.is_alive()

        lifetime.callback(join_server_thread)
        lifetime.callback(server.shutdown)
        yield f"http://127.0.0.1:{server.server_port}/v1", requests


@pytest.fixture
def shutdown_environment(
    integration_workspace_config_path: str, integration_workspace_root_path: str,
    model_replay: tuple[str, list[dict[str, object]]], model_protocol: str,
) -> Iterator[dict[str, str]]:
    config_path = Path(integration_workspace_config_path)
    before = config_path.read_bytes()
    config = read_jsonc_object(config_path)
    primary = next(provider for provider in config["llm"]["providers"] if provider["id"] == "primary")
    target = primary if model_protocol == "chat_completions" else next(
        provider for provider in config["llm"]["providers"] if provider["id"] == "backup_3"
    )
    primary.update({
        "endpoint": model_replay[0], "model": target["model"], "api_key": "fixture-key",
        "api_mode": target["api_mode"],
    })
    config["agents"]["default"]["model"]["fallback_providers"] = []
    validate_config(config, schema_path=Path.cwd() / "configs/workspace_schema.jsonc")
    with ExitStack() as lifetime:
        lifetime.callback(config_path.write_bytes, before)
        config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2))
        temporary = Path(integration_workspace_root_path).parent / "runtime/tmp"
        temporary.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(temporary, os.O_RDONLY | os.O_DIRECTORY)
        lifetime.callback(os.close, descriptor)
        short_path = f"/proc/{os.getpid()}/fd/{descriptor}"
        assert Path(short_path).resolve() == temporary
        yield {"TMPDIR": short_path, "TMP": short_path, "TEMP": short_path}


@pytest.mark.skipif(os.name != "posix", reason="该集成验收使用 Linux 精确 PID 与目录 fd")
@pytest.mark.asyncio
async def test_non_preserve_gateway_shutdown_drains_resources_before_auxiliary_exit(
    request: pytest.FixtureRequest, integration_workspace_root_path: str,
    shutdown_environment: dict[str, str], model_replay: tuple[str, list[dict[str, object]]],
) -> None:
    workspace = Path(integration_workspace_root_path)
    output = workspace.parent / "artifacts"
    output.mkdir(exist_ok=True)
    ports = integration_port_block_for_file(Path(request.node.path))
    gateway = start_gateway_process(
        workspace_root=workspace, default_backend_url=None, port=ports.port(21),
        extra_env=shutdown_environment | {"BOXTEAM_DEFAULT_BACKEND_PORT": str(ports.port(10))},
    )
    before: list[dict[str, object]] = []
    pids: dict[str, int] = {}
    pid_instances: dict[int, str] = {}
    group_members: dict[int, tuple[int, str]] = {}
    try:
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{gateway.port}", headers=LOCAL_TOKEN_HEADERS,
            timeout=90, trust_env=False,
        ) as client:
            await acquire_gateway_guest(client)
            reply = await client.get("/api/gateway/workspaces")
            assert reply.status_code == 200, reply.text
            data = reply.json()["data"]
            target = next(item for item in data["items"] if item["workspace_id"] == data["active_workspace_id"])
            for service in ("workspace_api", "terminal_manager", "browser_manager"):
                url = target["services"][service]["local_url"]
                health = await client.get(
                    url + ("/api/v1/health" if service == "workspace_api" else "/health"),
                    headers={"X-BoxTeam-Workspace-Id": target["backend_workspace_id"]},
                )
                assert health.status_code == 200, health.text
                pids[service] = health.json()["process_id"]
                pid = pids[service]
                pid_instances[pid] = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19]
            created = await client.post("/api/v1/sessions", json={"title": "整组关闭集成验收"})
            assert created.status_code == 200, created.text
            session = created.json()["data"]
            sid = session["session_id"]
            sent = await client.post(f"/api/v1/sessions/{sid}/messages", json={
                "message": {"content": "只回复就绪"}, "run": {"async": True},
            })
            assert sent.status_code == 200, sent.text
            job_id = sent.json()["data"]["job_id"]
            deadline = time.monotonic() + 60
            while True:
                job_reply = await client.get(f"/api/v1/jobs/{job_id}")
                assert job_reply.status_code == 200, job_reply.text
                job = job_reply.json()["data"]
                assert job["status"] not in {"failed", "cancelled", "timed_out"}, json.dumps(job, ensure_ascii=False)
                if job["status"] in {"completed", "succeeded"}:
                    break
                assert time.monotonic() < deadline, job
                await asyncio.sleep(0.05)
            assert model_replay[1], "实际 Job 必须发送到明确的模型替身"
            for kind in ("terminal", "browser"):
                for scope in ("thread", "workspace"):
                    reply = await client.post(f"/api/v1/resources/{kind}s", json={
                        "session_id": sid, "retention_scope": scope,
                    })
                    assert reply.status_code == 200, reply.text
                    resource = reply.json()["data"]
                    assert len(resource["thread_associations"]) == 1
                    assert resource["reconciliation_status"] == "verified"
                    before.append(resource)
            group_members = _active_group_members(set(pids.values()))
            assert set(pids.values()) <= set(group_members)
            (output / "before-shutdown.json").write_text(json.dumps({
                "resources": before, "pids": pids, "group_members": group_members,
            }, indent=2))
        # 默认 lifespan 关闭，不调用 restart-safe 或 preserve，也不主动删除资源。
        gateway.process.terminate()
        await asyncio.to_thread(gateway.process.wait, timeout=90)
        assert gateway.process.returncode in {0, -15}
        after = []
        for kind in ("terminal", "browser"):
            records = json.loads((workspace / ".boxteam" / f"{kind}-resources.json").read_text())["records"]
            for original in (item for item in before if item["kind"] == kind):
                record = next(item for item in records if item["resource_id"] == original["resource_id"])
                assert record["reconciliation_status"] == "verified", record
                assert record["thread_associations"] == [], record
                assert record["owner_revision"] > original["owner_revision"], record
                assert record["status"] == ("deleted" if original["retention_scope"] == "thread" else "running"), record
                if original["retention_scope"] == "workspace":
                    node_state = json.loads((workspace / ".boxteam" / f"{kind}-manager/{kind}s.json").read_text())
                    observed = next(item for item in node_state[f"{kind}s"] if item[f"{kind}_id"] == record["resource_id"])
                    assert observed["transport_owner_revision"] == record["owner_revision"], observed
                after.append(record)
        logs = workspace.parent / "boxteam-home/state/gateway/logs"
        combined = "\n".join(path.read_text(errors="replace") for path in logs.glob("*.log"))
        combined += (workspace / ".boxteam/logs/gateway.stderr.log").read_text()
        assert "Application shutdown failed" not in combined
        assert "ConnectionRefusedError" not in combined
        for pid in pids.values():
            assert not Path(f"/proc/{pid}").exists(), f"关闭后 owned PID 仍存在: {pid}"
        survivors = _active_group_members(set(pids.values()))
        assert survivors == {}, f"关闭后 owned process group 仍有活进程: {survivors}"
        (output / "after-shutdown.json").write_text(json.dumps({
            "resources": after, "pids": pids, "group_members": survivors,
        }, indent=2))
    finally:
        close_gateway_process(gateway)
        # 失败时重新观察原服务组，收回关闭期间新加入的成员；拒绝复用 leader。
        for pid, started in pid_instances.items():
            try:
                actual = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19]
            except FileNotFoundError:
                continue
            assert actual == started, f"owned group leader 已被替换，拒绝清理: {pid}"
        group_members.update(_active_group_members(set(pids.values())))
        # 失败清理只回收健康握手登记的同一 PID 实例，绝不按端口找进程。
        for pid, started in pid_instances.items():
            try:
                actual = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19]
            except FileNotFoundError:
                continue
            if actual == started and os.getpgid(pid) == pid:
                os.killpg(pid, 9)
        for pid, (group, started) in group_members.items():
            try:
                fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
            except FileNotFoundError:
                continue
            if fields[19] == started and fields[0] not in {"Z", "X"} and os.getpgid(pid) == group:
                os.killpg(group, 9)
