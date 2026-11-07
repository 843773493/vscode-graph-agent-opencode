"""真实 Gateway／目录安装／source CAS／原 Thread 的 stdio 私有 env 纵切。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import sys
import time
from pathlib import Path
from typing import cast

import commentjson
import httpx
import pytest

from app.testing.model_stream import build_cassette, chat_completion_frames
from tests.integration.backend.agents.test_debug_prompt_flow import _tool_call
from tests.integration.gateway.test_mcp_artifact_installation import (
    ArtifactIndex,
    _wheel,
)
from tests.integration.gateway.test_mcp_artifact_installation import (
    artifact_download_gate as artifact_download_gate,  # noqa: PLC0414 - 显式复用外部来源装具
)
from tests.integration.gateway.test_mcp_artifact_installation import (
    artifact_index as artifact_index,  # noqa: PLC0414 - 不通过测试模块插件隐式注册
)
from tests.integration.gateway.test_mcp_private_headers import (
    MODEL,
    MODEL_ENDPOINT,
    SOURCE,
    PrivateHeaderCase,
)
from tests.integration.gateway.test_mcp_private_headers import (
    http_schema_endpoint_factory as http_schema_endpoint_factory,  # noqa: PLC0414 - 原 HTTP MCP 装具依赖
)
from tests.integration.gateway.test_mcp_private_headers import (
    private_header_case as private_header_case,  # noqa: PLC0414 - 同一工作区隔离与模型回放
)
from tests.support.api_helpers import create_session
from tests.support.api_waiters import wait_for_job_done
from tests.support.processes.gateway import LOCAL_TOKEN_HEADERS

TOKEN_KEY = "MCP_PRIVATE_TOKEN"
RECEIPT_KEY = "MCP_ENV_RECEIPTS"


@pytest.fixture
def private_env_catalog(artifact_index: ArtifactIndex) -> ArtifactIndex:
    # 只准备外部 Registry／标准 wheel 输入，安装和发布由 Gateway 产品入口生成。
    code = (Path.cwd() / "tests/integration/workspace_services/mcp/private_env_server.py").read_bytes()
    _wheel(artifact_index.root, "private-env-server", "1.0.0", code, entrypoint="private-env-server")
    for name in ("org.example/artifact", "org.example/changed"):
        manifest = artifact_index.root / "v0.1/servers" / name / "versions/release-1"
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text(json.dumps({"server": {"name": name, "version": "release-1", "packages": [{
            "registryType": "pypi", "identifier": "private-env-server", "version": "1.0.0",
            "registryBaseUrl": artifact_index.url + "/simple", "transport": {"type": "stdio"},
            "environmentVariables": [{"name": TOKEN_KEY, "isSecret": True, "isRequired": True},
                {"name": RECEIPT_KEY, "isRequired": True}],
        }]}, "_meta": {}}), encoding="utf-8")
    return artifact_index


async def test_private_env_survives_original_thread_restart_and_rejects_borrowed_launch_or_source(
    private_header_case: PrivateHeaderCase, private_env_catalog: ArtifactIndex,
) -> None:
    case, index = private_header_case, private_env_catalog
    states: dict[str, dict] = {}
    gateway_id = (await case.client.get("/api/gateway/health")).json()["data"]["gateway_id"]
    discovery = await case.client.post("/api/gateway/mcp-installation-targets", json={"gateway_id": gateway_id})
    discovery.raise_for_status()
    backend, = discovery.json()["data"]
    installation_base = f"/api/gateway/mcp/{gateway_id}/{backend['backend_id']}"

    async def exchange(method: str, path: str, body: dict | None = None, *,
        status: int = 200, headers: dict[str, str] | None = None, envelope: bool = True) -> dict:
        request_id = f"private-env-{len(case.evidence)}"
        response = await case.client.request(method, path, json=body,
            headers={**(headers or {}), "X-Request-ID": request_id})
        case.require_public(response.content)
        payload = response.json()
        assert response.headers["X-Request-ID"] == request_id
        if envelope:
            assert payload["request_id"] == request_id
        case.evidence.append({"method": method, "path": path, "status": response.status_code,
            "request_id": request_id, "body": payload})
        assert response.status_code == status, response.text
        return payload

    async def edit_source(base: str, servers: dict | None, key: str) -> dict:
        document = (await exchange("GET", base + "/config/sources/" + SOURCE))["data"]
        config = commentjson.loads(document["jsonc"])
        if servers is not None:
            config["mcp"] = {"servers": servers}
        return {"gateway_id": document["gateway_id"], "workspace_id": document["workspace_id"], "source_key": SOURCE,
            "jsonc": document["jsonc"] if servers is None else json.dumps(config, ensure_ascii=False, indent=2) + "\n", "base_file_digest": document["file_digest"],
            "base_layer_revision": document["layer_revision"], "base_layer_digest": document["layer_digest"],
            "expected_active_revision": document["active_revision"], "expected_active_digest": document["active_digest"],
            "idempotency_key": key}

    async def install(candidate: dict, plan_digest: str, operation_id: str) -> dict:
        operation = (await exchange("POST", installation_base + "/installations", {
            "candidate": candidate, "plan_digest": plan_digest, "operation_id": operation_id,
            "entrypoint": "private-env-server"}))["data"]
        deadline = time.monotonic() + 60
        while operation["status"] == "running":
            assert time.monotonic() < deadline, operation
            await asyncio.sleep(0.1)
            operation = (await exchange("GET", installation_base + "/installation-operations/" + operation_id))["data"]
        assert operation["status"] == "succeeded", operation
        return operation["artifact"]

    async def invoke(label: str, server: str, phase: str) -> None:
        state = states[label]
        proof = f"{label.lower()}-{server}-{phase}"
        tool_id = f"mcp__{server}__private_env_probe"
        search = {**_tool_call(call_index=0, name="search_tools", arguments={"tool_ids": [tool_id]}), "id": "env-search-" + proof}
        call = {**_tool_call(call_index=1, name="invoke_extension_tool", arguments={
            "tool_name": tool_id, "arguments": {"proof": proof}}), "id": "env-invoke-" + proof}
        interactions = [cast(list[dict[str, object]], build_cassette(asset_id=f"private-env-{proof}-{number}", model=MODEL,
            url=MODEL_ENDPOINT + "/chat/completions", frames=chat_completion_frames(
                response_id=f"chatcmpl-private-env-{proof}-{number}", model=MODEL,
                deltas=({"role": "assistant", **delta},), finish_reason="tool_calls" if "tool_calls" in delta else "stop",
            )).raw["interactions"])[0] for number, delta in enumerate([
                {"tool_calls": [search]}, {"tool_calls": [call]}, {"content": "PRIVATE_ENV_OK " + proof},
            ])]
        calls = case.replay.control.plan_model_calls(state["session_id"], state["thread_id"], interactions)
        async with httpx.AsyncClient(base_url=case.client.base_url, cookies=case.client.cookies,
            headers={**LOCAL_TOKEN_HEADERS, **state["selection"]}, timeout=60, trust_env=False) as selected:
            accepted = await selected.post(f"/api/v1/sessions/{state['session_id']}/messages", json={
                "message": {"content": f"发现并调用 {tool_id}，proof={proof}，读取回执后结束。"},
                "run": {"mode": "single_agent", "agent_id": "default"}})
            case.require_public(accepted.content)
            assert accepted.status_code == 200, accepted.text
            job = await wait_for_job_done(selected, accepted.json()["data"]["job_id"], max_attempts=90)
            case.require_public(json.dumps(job, ensure_ascii=False).encode("utf-8"))
        records = [json.loads((case.replay.artifacts / f"{call_id}.json").read_text(encoding="utf-8")) for call_id in calls]
        case.require_public(json.dumps(records, ensure_ascii=False).encode("utf-8"))
        result, = [message for message in records[-1]["request"]["messages"]
            if message.get("role") == "tool" and message.get("tool_call_id") == call["id"]]
        part, = result["content"]
        expected = {"proof": proof, "secret_digest": state["secret_digest"],
            "prefix": str(Path(state["artifact"]["command"]).parent.parent) if server == "secure" else sys.prefix}
        assert part["type"] == "text" and json.loads(part["text"]) == expected
        receipts = [json.loads(line) for line in state["receipts"].read_text(encoding="utf-8").splitlines()]
        assert [row for row in receipts if row["proof"] == proof] == [
            {"kind": "called", "proof": proof, "secret_digest": state["secret_digest"]}]
        case.evidence.append({"phase": proof, "session_id": state["session_id"], "thread_id": state["thread_id"],
            "model_call_ids": calls, "tool_result": expected})

    listing = (await exchange("GET", "/api/gateway/workspaces"))["data"]
    first, = listing["items"]
    first_id = first["workspace_id"]
    await exchange("GET", f"/api/v1/workspaces/{first_id}/workspace")
    listing = (await exchange("POST", "/api/gateway/managed-workspaces", {
        "root_path": str(case.workspaces["A2"]), "name": "私有 env A2"}))["data"]
    second_id, = [item["workspace_id"] for item in listing["items"] if item["workspace_id"] != first_id]
    for label, workspace_id in (("A1", first_id), ("A2", second_id)):
        base = f"/api/v1/workspaces/{workspace_id}"
        await exchange("GET", base + "/workspace")
        document = (await exchange("GET", base + "/config/sources/" + SOURCE))["data"]
        target = {"kind": "workspace", "gateway_id": document["gateway_id"], "workspace_id": workspace_id}
        selection = {"X-BoxTeam-Workspace-Id": workspace_id, "X-BoxTeam-Target-Gateway-Id": target["gateway_id"]}
        capabilities = await exchange("GET", installation_base + "/installation-capabilities")
        assert capabilities["data"]["pypi_wheel_available"] is True
        assert backend == capabilities["target"]
        inspected = (await exchange("POST", installation_base + "/candidate-manifests", {
            "target": backend, "source": index.source()}))["data"]
        receipts = case.workspaces[label] / "private-env-observations.jsonl"
        candidate = {"target": backend, "source": index.source(), "manifest_digest": inspected["manifest_digest"],
            "variant": inspected["variants"][0]["ref"], "server_id": "secure", "inputs": {RECEIPT_KEY: str(receipts)}}
        installation_candidate = {**candidate, "inputs": {}}
        installation_plan = (await exchange("POST", installation_base + "/candidate-plans", installation_candidate))["data"]
        artifact = await install(installation_candidate, installation_plan["plan_digest"], "private-env-install-" + label)
        private_path = "/api/v1/mcp/candidate-plans/private-env" if label == "A1" else base + "/mcp/candidate-plans/private-env"
        reference = (await exchange("POST", private_path, {"target": target, "server_id": "secure", "input_key": TOKEN_KEY,
            "candidate": candidate, "value": case.private_values[label]}, headers=selection))["data"]
        assert reference["workspace_id"] == workspace_id and reference["server_id"] == "secure" and reference["input_key"] == TOKEN_KEY
        candidate["inputs"][TOKEN_KEY] = reference
        plan = (await exchange("POST", base + "/mcp/candidate-configurations/plans", candidate))["data"]
        assert plan["parameters"]["env"][TOKEN_KEY] == reference and str(receipts) not in json.dumps(plan)
        directory = {"enabled": True, "transport": "stdio", "managed_launch": artifact["binding"], "args": [],
            "env": {TOKEN_KEY: reference, RECEIPT_KEY: str(receipts)}}
        edit = await edit_source(base, None, label + "-catalog-source")
        source_file = case.workspaces[label] / ".boxteam/workspace.jsonc"
        original_bytes = source_file.read_bytes()
        draft = (await exchange("POST", base + "/mcp/candidate-configurations/draft", {
            "target": target, "server_id": "secure", "plan": {"candidate": candidate, "plan_digest": plan["plan_digest"]},
            "managed_launch": artifact["binding"], "source_edit": edit, "enabled": True}))["data"]
        assert source_file.read_bytes() == original_bytes
        assert str(receipts) not in json.dumps(draft)
        assert draft["preflight"]["document"]["file_digest"] == edit["base_file_digest"]
        generated = commentjson.loads(draft["source_edit"]["jsonc"])["mcp"]["servers"]["secure"]
        assert generated["managed_launch"] == artifact["binding"] and generated["env"][TOKEN_KEY] == reference
        assert generated["env"][RECEIPT_KEY].startswith("boxteam-redacted:")
        configuration = {"target": target, "server_id": "secure", "plan": {"candidate": candidate, "plan_digest": plan["plan_digest"]},
            "managed_launch": artifact["binding"], "source_edit": draft["source_edit"]}
        saved = (await exchange("PUT", base + "/mcp/candidate-configurations", configuration))["data"]
        assert saved["edit"]["state"] == "active" and saved["edit"]["source_saved"] and saved["edit"]["layer_imported"]
        assert json.loads(saved["jsonc"])["mcp"]["servers"]["secure"]["env"][TOKEN_KEY] == reference
        persisted = commentjson.loads(source_file.read_text(encoding="utf-8"))["mcp"]["servers"]["secure"]
        assert persisted == directory
        retried = (await exchange("PUT", base + "/mcp/candidate-configurations", configuration))["data"]
        assert retried["edit"] == saved["edit"] and retried["active_revision"] == saved["active_revision"]
        before_stale = (case.workspaces[label] / ".boxteam/workspace.jsonc").read_bytes()
        await exchange("PUT", base + "/mcp/candidate-configurations", {**configuration,
            "source_edit": {**draft["source_edit"], "idempotency_key": label + "-stale-source"}}, status=409)
        assert (case.workspaces[label] / ".boxteam/workspace.jsonc").read_bytes() == before_stale
        # 手动场景直接运行独立脚本，避免绕过 managed_launch 的隔离参数污染已发布 wheel。
        launch = {"command": sys.executable, "args": [str(Path.cwd() / "tests/integration/workspace_services/mcp/private_env_server.py")],
            "cwd": str(case.workspaces[label])}
        manual_ref = (await exchange("POST", base + "/mcp/private-env", {"target": target, "server_id": "manual",
            "input_key": TOKEN_KEY, "launch": launch, "value": case.private_values[label]}))["data"]
        manual = {"enabled": True, "transport": "stdio", **launch, "env": {TOKEN_KEY: manual_ref, RECEIPT_KEY: str(receipts)}}
        edit = await edit_source(base, {"secure": directory, "manual": manual}, label + "-manual-source")
        await exchange("POST", base + "/config/sources/" + SOURCE + "/preflight", edit)
        saved = (await exchange("PUT", base + "/config/sources/" + SOURCE, edit))["data"]
        assert saved["edit"]["state"] == "active" and saved["edit"]["source_saved"] and saved["edit"]["layer_imported"]
        source = {"kind": "manual", "name": "private-env-manual", "version": "1", "definition": manual}
        inspected = (await exchange("POST", installation_base + "/candidate-manifests", {"target": backend, "source": source}))["data"]
        manual_candidate = {"target": backend, "source": source, "manifest_digest": inspected["manifest_digest"],
            "variant": inspected["variants"][0]["ref"], "server_id": "manual"}
        manual_plan = (await exchange("POST", base + "/mcp/candidate-configurations/plans", manual_candidate))["data"]
        servers = (await exchange("GET", base + "/mcp/servers"))["data"]
        assert {server["server_id"] for server in servers} == {"secure", "manual"} and all(server["status"] == "ready" for server in servers)
        health = await exchange("GET", base + "/health", envelope=False)
        async with httpx.AsyncClient(base_url=case.client.base_url, cookies=case.client.cookies,
            headers={**LOCAL_TOKEN_HEADERS, **selection}, timeout=60, trust_env=False) as selected:
            session = await create_session(selected, "MCP 私有 env " + label)
            response = await selected.get(f"/api/v1/sessions/{session}/main-thread")
            response.raise_for_status()
            thread = response.json()["data"]["thread_id"]
        states[label] = {"base": base, "target": target, "backend": backend, "selection": selection, "candidate": candidate,
            "plan_digest": plan["plan_digest"], "artifact": artifact, "reference": reference, "manual": manual,
            "installation_candidate": installation_candidate, "installation_plan_digest": installation_plan["plan_digest"],
            "directory": directory, "manual_candidate": manual_candidate, "manual_plan": manual_plan,
            "receipts": receipts, "health": health, "session_id": session, "thread_id": thread,
            "secret_digest": hashlib.sha256(case.private_values[label].encode("utf-8")).hexdigest()}
        for server in ("secure", "manual"):
            await invoke(label, server, "before")
    assert states["A1"]["health"]["process_id"] != states["A2"]["health"]["process_id"]
    assert states["A1"]["secret_digest"] != states["A2"]["secret_digest"]
    assert states["A1"]["reference"]["version"] != states["A2"]["reference"]["version"]

    for label, state in states.items():
        workspace_id = state["target"]["workspace_id"]
        listing = (await exchange("GET", "/api/gateway/workspaces"))["data"]
        item, = [item for item in listing["items"] if item["workspace_id"] == workspace_id]
        assert item["affected_workspace_ids"] == [workspace_id]
        restarted = (await exchange("POST", f"/api/gateway/workspaces/{workspace_id}/runtime/restart-safe", {
            "expected_backend_generation": item["backend_generation"], "expected_workspace_ids": item["affected_workspace_ids"]}))["data"]
        assert restarted["status"] == "restarted" and restarted["affected_workspace_ids"] == [workspace_id]
        health = await exchange("GET", state["base"] + "/health", envelope=False)
        assert health["process_id"] != state["health"]["process_id"] and health["backend_id"] == state["health"]["backend_id"]
        restored = (await exchange("GET", state["base"] + "/config/sources/" + SOURCE))["data"]
        assert json.loads(restored["jsonc"])["mcp"]["servers"]["secure"]["env"][TOKEN_KEY] == state["reference"]
        reused = await install(state["installation_candidate"], state["installation_plan_digest"], "private-env-reinstall-" + label)
        assert reused["binding"] == state["artifact"]["binding"]
        case.evidence.append({"phase": label + "-same-source-warm-reinstall", "binding": reused["binding"], "new_generation": False})
        for server in ("secure", "manual"):
            await invoke(label, server, "after")
    # TODO(3.2)：当前真实安装入口会复用同源 generation；新增合法重新发布入口后再验换代复用引用，不能手改安装账本冒充重装。
    case.evidence.append({"pending": "same-source-new-generation", "reason": "现有真实安装入口仅复用原 generation"})

    originals = {label: (case.workspaces[label] / ".boxteam/workspace.jsonc").read_bytes() for label in states}
    receipts_before = {label: state["receipts"].read_bytes() for label, state in states.items()}
    second = states["A2"]
    borrowed = {**second["directory"], "env": {**second["directory"]["env"], TOKEN_KEY: states["A1"]["reference"]}}
    edit = await edit_source(second["base"], {"secure": borrowed, "manual": second["manual"]}, "reject-workspace-borrow")
    rejected = await exchange("PUT", second["base"] + "/mcp/candidate-configurations", {"target": second["target"], "server_id": "secure",
        "plan": {"candidate": second["candidate"], "plan_digest": second["plan_digest"]},
        "managed_launch": second["artifact"]["binding"], "source_edit": edit}, status=422)
    assert "不属于" in json.dumps(rejected, ensure_ascii=False)
    first = states["A1"]
    changed_source = {**index.source(), "name": "org.example/changed",
        "entry_url": index.url + "/v0.1/servers/org.example%2Fchanged/versions/release-1"}
    inspected = (await exchange("POST", installation_base + "/candidate-manifests", {"target": first["backend"], "source": changed_source}))["data"]
    rejected = await exchange("POST", first["base"] + "/mcp/candidate-configurations/plans", {**first["candidate"], "source": changed_source,
        "manifest_digest": inspected["manifest_digest"], "variant": inspected["variants"][0]["ref"]}, status=422)
    assert "不属于" in json.dumps(rejected, ensure_ascii=False)
    changed_launch = {**first["manual"], "command": first["artifact"]["command"], "args": []}
    edit = await edit_source(first["base"], {"secure": first["directory"], "manual": changed_launch}, "reject-command-change")
    rejected = await exchange("PUT", first["base"] + "/mcp/candidate-configurations", {"target": first["target"], "server_id": "manual",
        "plan": {"candidate": first["manual_candidate"], "plan_digest": first["manual_plan"]["plan_digest"]}, "source_edit": edit}, status=422)
    assert "不属于" in json.dumps(rejected, ensure_ascii=False)
    for label, state in states.items():
        assert (case.workspaces[label] / ".boxteam/workspace.jsonc").read_bytes() == originals[label]
        assert state["receipts"].read_bytes() == receipts_before[label]
        receipts = [json.loads(line) for line in receipts_before[label].decode("utf-8").splitlines()]
        calls = [receipt for receipt in receipts if receipt["kind"] == "called"]
        assert len(calls) == 4 and all(row["secret_digest"] == state["secret_digest"] for row in receipts)
