"""在完整旧源码中经 HTTP 生成身份迁移输入；不直接写入业务关联。"""

from __future__ import annotations

import argparse
import hashlib
import json
import signal
import sqlite3
import time
from contextlib import ExitStack, closing
from pathlib import Path
from typing import cast

import httpx


def data(response: httpx.Response) -> dict:
    assert response.status_code == 200, response.text
    payload = response.json()
    assert response.headers["X-Request-ID"] == payload["request_id"]
    return payload["data"]


def business_snapshot(workspace: Path, workspace_id: str, session_id: str,
                      thread_id: str, captures: dict[str, str]) -> dict:
    """只读原 catalog locator、canonical 正文和已封存请求，不准备任何状态。"""
    from tests.support.sealed_request_audit import audit_sealed_requests
    from tests.support.session_catalog_owner import fixture_catalog_scope

    with fixture_catalog_scope() as owner:
        resolver = owner.get_catalog_components(workspace / ".boxteam/sessions", workspace_id).resolver
        node = resolver.resolve_session_node(session_id)
        thread_node = resolver.resolve_thread_node(session_id, thread_id)
    index = thread_node / "rollout/index.sqlite"
    logs = sorted((node / "logs/llm_requests").glob("*.json"))
    assert logs, "旧产品必须产生真实 Provider 日志"
    observed = {call: json.loads(Path(path).read_text(encoding="utf-8")) for call, path in captures.items()}
    audit = audit_sealed_requests(index, logs, observed)
    with closing(sqlite3.connect(index.as_uri() + "?mode=ro", uri=True)) as connection:
        items = connection.execute(
            "SELECT item_id,item_sequence,content_hash,source_revision FROM item_catalog ORDER BY item_sequence"
        ).fetchall()
    assert items and audit["requests"], "不能用空历史证明保留 hash"
    paths = [node / "session.json", thread_node / "rollout/rollout.jsonl", *logs]
    return {"session_node": str(node), "thread_node": str(thread_node), "audit": audit,
            "canonical_items": [list(row) for row in items],
            "file_hashes": {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}}


def generate(workspace: Path, home: Path, artifacts: Path, runtime: Path,
             backend_port: int, gateway_port: int, model_port: int) -> dict:
    # 此脚本由当前测试传入完整旧 archive 执行；所有产品与装具 import 均来自旧根。
    import app
    import tests.support.processes.gateway as old_gateway
    from app.testing.model_stream import build_cassette, chat_completion_frames
    from configs.runtime import read_jsonc_object
    from tests.support.model_stream.config import model_stream_replay
    from tests.support.processes.workspace import (
        close_backend_process,
        start_backend_process,
    )

    source = Path.cwd().resolve()
    assert Path(cast(str, app.__file__)).resolve().is_relative_to(source)
    assert Path(cast(str, old_gateway.__file__)).resolve().is_relative_to(source)
    workspace.mkdir(parents=True, exist_ok=True)
    config_root = workspace / ".boxteam"
    config_root.mkdir(exist_ok=True)
    (workspace / "README.md").write_text("# 旧版本真实迁移输入\n", encoding="utf-8")
    config = read_jsonc_object(source / "configs/tests/workspace/default.jsonc")
    providers = cast(list[dict], cast(dict, config["llm"])["providers"])
    model_url = f"http://127.0.0.1:{model_port}/v1/chat/completions"
    for provider in providers:
        provider.update(endpoint=f"http://127.0.0.1:{model_port}/v1", model="e2e-stub-model",
                        api_key="e2e-local-model-key", custom_llm_provider="openai", api_mode={
                            "protocol": "chat_completions",
                            "model_info": {"supports_function_calling": True, "supports_reasoning": False},
                            "supports_reasoning": {"reasoning_content": False},
                        })
        provider.pop("auth", None)
        provider.pop("request_options", None)
    for agent in cast(dict[str, dict], config["agents"]).values():
        cast(dict, agent["model"])["fallback_providers"] = []
    config["$schema"] = str(source / "configs/workspace_schema.jsonc")
    (config_root / "workspace.jsonc").write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
    artifacts.mkdir(parents=True, exist_ok=True)
    with ExitStack() as lifetime:
        replay = lifetime.enter_context(model_stream_replay(
            artifacts, runtime_root=runtime, business_assertion="legacy-owner-history-survives-identity-cli-and-restart",
        ))
        backend = start_backend_process(
            workspace_root=str(workspace), port=backend_port, log_name="legacy-backend", boxteam_home=home,
            artifacts_dir=artifacts, asgi_app="tests.support.e2e_control.backend_app:app",
            env_overrides={**replay.environment, "LITELLM_LOCAL_MODEL_COST_MAP": "true",
                           "BOXTEAM_GATEWAY_URL": f"http://127.0.0.1:{gateway_port}"},
        )
        lifetime.callback(close_backend_process, backend)
        gateway = old_gateway.start_gateway_process(
            workspace_root=workspace, default_backend_url=f"http://127.0.0.1:{backend_port}",
            port=gateway_port, boxteam_home=home, artifacts_dir=artifacts,
        )
        lifetime.callback(old_gateway.close_gateway_process, gateway)
        client = lifetime.enter_context(httpx.Client(
            base_url=f"http://127.0.0.1:{gateway_port}", headers=old_gateway.LOCAL_TOKEN_HEADERS, timeout=30,
        ))
        workspaces = data(client.get("/api/gateway/workspaces"))
        alias = workspaces["active_workspace_id"]
        target, = workspaces["items"]
        assert target["workspace_id"] == alias and target["backend_workspace_id"] == backend.workspace_id
        assert alias.startswith("gw_") and alias != backend.workspace_id
        user = data(client.post("/api/gateway/users", json={"display_name": "旧版本迁移用户"}))
        data(client.post(f"/api/gateway/users/{user['user_id']}/access", json={"client_label": "legacy-migration"}))
        client.headers["X-BoxTeam-Workspace-Id"] = alias
        session = data(client.post("/api/v1/sessions", json={"title": "旧产品生成的会话"}))
        session_id = session["session_id"]
        thread = data(client.get(f"/api/v1/sessions/{session_id}/main-thread"))
        thread_id = thread["thread_id"]
        prompt = f"原正文保留 {alias} 与 boxteam://workspace/{backend.workspace_id}/resources/files/root/README.md"
        answer = f"旧版本真实执行已完成；原别名 {alias} 仍属于历史正文。"
        cassette = build_cassette(
            asset_id="workspace-identity-migration-restart", model="e2e-stub-model", url=model_url,
            frames=chat_completion_frames(
                response_id="chatcmpl-legacy-identity", model="e2e-stub-model",
                deltas=({"role": "assistant", "content": answer},), finish_reason="stop",
            ),
        )
        interactions = cast(list[dict[str, object]], cassette.raw["interactions"])
        call, = replay.control.plan_model_calls(session_id, thread_id, interactions)
        accepted = data(client.post(f"/api/v1/sessions/{session_id}/messages", json={
            "message": {"content": prompt}, "run": {"mode": "single_agent", "agent_id": "default"},
        }))
        deadline = time.monotonic() + 60
        while True:
            job = data(client.get(f"/api/v1/jobs/{accepted['job_id']}"))
            if job["status"] in {"completed", "failed", "cancelled", "timed_out", "interrupted"}:
                assert job["status"] == "completed", job
                break
            assert time.monotonic() < deadline, job
            time.sleep(0.1)
        session = data(client.get(f"/api/v1/sessions/{session_id}"))
        history = data(client.post(f"/api/v1/sessions/{session_id}/history", json={"direction": "tail"}))
        turn, = history["items"]
        assert turn["final_response"] == answer
        messages = data(client.get(f"/api/v1/sessions/{session_id}/messages"))
        assert any(item["role"] == "user" and item["content"] == prompt for item in messages["items"])
        view = data(client.put("/api/gateway/users/current/view-state", params={
            "workspace_id": alias, "session_id": session_id,
        }, json={"turn_anchor": turn["turn_id"], "scroll_offset": 123.5, "follow_latest": False,
                 "projection_version": 7, "tool_details_expanded": True}))
        tree = data(client.post("/api/gateway/workspace-navigation/folders", json={"name": "迁移前的父文件夹"}))
        folder, = [node for node in tree["nodes"] if node["kind"] == "workspace_folder"]
        reference, = [node for node in tree["nodes"] if node["kind"] == "workspace_ref"]
        navigation = data(client.patch(f"/api/gateway/workspace-navigation/nodes/{reference['node_id']}",
                                      json={"parent_node_id": folder["node_id"]}))
        capture = replay.artifacts / f"{call}.json"
        assert json.loads(capture.read_text(encoding="utf-8"))["identity"] == {
            "session_id": session_id, "thread_id": thread_id, "model_call_id": call,
        }
        pids = {"backend": backend.process.pid, "gateway": gateway.process.pid}
        data(client.delete("/api/gateway/users/current"))
    # 必须等两个旧进程退出后才冻结控制面与历史；不清洗 runtime journal 来帮助迁移。
    # Uvicorn 完成 lifespan 后重发原 SIGTERM；不接受超时强杀的 SIGKILL。
    assert backend.process.returncode in (0, -signal.SIGTERM)
    assert gateway.process.returncode in (0, -signal.SIGTERM)
    gateway_root = home / "state/gateway"
    with closing(sqlite3.connect(gateway_root / "gateway.sqlite")) as connection:
        registry = json.loads(connection.execute(
            "SELECT payload_json FROM gateway_config WHERE config_key='workspace_registry_meta'"
        ).fetchone()[0])
        schema = connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
        target_record = json.loads(connection.execute("SELECT payload_json FROM gateway_workspace_registry").fetchone()[0])
        dump = "\n".join(connection.iterdump())
    assert registry["schema_version"] == 13 and schema == 18
    (artifacts / "gateway-before.sql").write_text(dump, encoding="utf-8")
    gateway_id = json.loads((gateway_root / "identity.json").read_text(encoding="utf-8"))["gateway_id"]
    captures = {call: str(capture)}
    result = {"source_root": str(source), "workspace_id": backend.workspace_id, "gateway_id": gateway_id,
              "legacy_alias": alias, "user": user, "session": session, "thread": thread,
              "history": history, "messages": messages, "view": view, "navigation": navigation,
              "target_record": target_record, "captures": captures, "pids": pids,
              "exit_codes": {"backend": backend.process.returncode, "gateway": gateway.process.returncode},
              "business": business_snapshot(workspace, backend.workspace_id, session_id, thread_id, captures),
              "identity_bytes": (config_root / "workspace-identity.json").read_text(encoding="utf-8")}
    (artifacts / "legacy-input.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("workspace", "home", "artifacts", "runtime"):
        parser.add_argument(f"--{name}", required=True, type=Path)
    for name in ("backend-port", "gateway-port", "model-port"):
        parser.add_argument(f"--{name}", required=True, type=int)
    args = parser.parse_args()
    generate(args.workspace, args.home, args.artifacts, args.runtime,
             args.backend_port, args.gateway_port, args.model_port)


if __name__ == "__main__":
    main()
