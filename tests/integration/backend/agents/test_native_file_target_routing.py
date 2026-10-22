"""原生路径经真实远端 Gateway 在固定目标解释；响应只使用资源 URI。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import sqlite3
from contextlib import closing

from app.core.identifier import create_prefixed_id
from app.core.resource_uri import parse_resource_uri, resource_display_uri
from tests.integration.backend.sessions.test_session_communication_pipeline import (
    CommunicationCase,
)
from tests.integration.backend.sessions.test_session_communication_pipeline import (
    communication_case as communication_case,  # noqa: PLC0414 - fixture 按当前文件隔离
)
from tests.integration.backend.sessions.test_session_communication_pipeline import (
    communication_triad as communication_triad,  # noqa: PLC0414 - fixture 按当前文件隔离
)

# TODO：Linux 跨 Gateway 三输入与 Windows 本机 Gateway 盘符读写/盘符相对路径拒绝已实测；跨平台联邦及真实 UNC 共享仍需验收。


async def test_remote_file_api_resolves_native_and_uri_targets_on_target_machine(communication_triad):
    case = communication_triad
    nodes, artifacts = case.nodes, case.artifacts
    source, target = nodes["a"], nodes["c"]
    relative = "src/原生 目标%.txt"
    originals = {}
    for name, node in nodes.items():
        path = node.workspace / relative
        originals[path] = (name + " OWNER Ω\n").encode()
        path.write_bytes(originals[path])
    native = target.workspace / relative
    uri = resource_display_uri(scope="workspace", scope_id=target.workspace_id, kind="files",
        tail_segments=("root", *native.relative_to(target.workspace).parts))
    # 旧去根回退会命中这些真实对照，不能把拒绝或成功误投到其它工作区。
    for node in (nodes["a"], nodes["b"]):
        decoy = node.workspace / str(native).lstrip("/")
        decoy.parent.mkdir(parents=True)
        originals[decoy] = b"REDIRECT DECOY\n"
        decoy.write_bytes(originals[decoy])
    registered = await source.client.get("/api/gateway/workspaces")
    assert registered.status_code == 200, registered.text
    routes = {row["backend_workspace_id"]: row["workspace_id"] for row in registered.json()["data"]["items"]}
    switched = await source.client.post(f"/api/gateway/workspaces/{routes[nodes['b'].workspace_id]}/activate")
    assert switched.status_code == 200, switched.text
    endpoint = f"/api/v1/workspaces/{target.workspace_id}/workspace/files/content"
    evidence = []

    async def request(method, path, *, status=200, body=None):
        response = await source.client.request(method, endpoint, params={"path": path}, json=body,
            headers={"X-BoxTeam-Workspace-Id": routes[nodes["b"].workspace_id]})
        assert response.status_code == status, response.text
        assert response.headers["X-Request-ID"] == response.json()["request_id"]
        for node in nodes.values():
            assert str(node.workspace) not in response.text and str(node.gateway_home) not in response.text
        assert "local-dev-token" not in response.text and "federation_token" not in response.text
        evidence.append({"method": method, "status": status, "response": response.json()})
        return response.json().get("data")

    values = (relative, str(native), uri)
    reads = [await request("GET", value) for value in values]
    assert all(read["path"] == uri and read["content"] == "c OWNER Ω\n" for read in reads)
    assert {read["revision"] for read in reads} == {hashlib.sha256(originals[native]).hexdigest()}
    revision = reads[0]["revision"]
    for index, value in enumerate(values):
        content = f"c UPDATED {index} Ω\n"
        saved = await request("PUT", value, body={"content": content, "expected_revision": revision})
        assert saved["path"] == uri and saved["content"] == content
        assert native.read_bytes() == content.encode()
        revision = saved["revision"]
        for other in values:
            reread = await request("GET", other)
            assert (reread["path"], reread["content"], reread["revision"]) == (uri, content, revision)

    # 外部绝对路径保留原 Web 文件能力，由目标 Gateway 绑定，不要求注册为工作区。
    external = target.workspace.parent / "外部 目标%.txt"
    external.write_text("EXTERNAL original\n", encoding="utf-8")
    with closing(sqlite3.connect((target.gateway_root / "gateway.sqlite").as_uri() + "?mode=ro")) as database:
        before = database.execute("SELECT * FROM gateway_workspace_registry ORDER BY workspace_id").fetchall()
    outside = await request("GET", str(external))
    parsed = parse_resource_uri(outside["path"])
    assert (parsed.scope, parsed.scope_id, parsed.kind) == ("gateway", target.gateway_id, "files")
    saved = await request("PUT", str(external), body={"content": "EXTERNAL saved\n", "expected_revision": outside["revision"]})
    assert saved["path"] == outside["path"] and external.read_text(encoding="utf-8") == "EXTERNAL saved\n"
    assert (await request("GET", outside["path"]))["content"] == "EXTERNAL saved\n"
    with closing(sqlite3.connect((target.gateway_root / "gateway.sqlite").as_uri() + "?mode=ro")) as database:
        assert database.execute("SELECT * FROM gateway_workspace_registry ORDER BY workspace_id").fetchall() == before

    await asyncio.to_thread(target.restart_backend, "kill")
    await request("GET", relative, status=502)
    await asyncio.to_thread(target.restart_backend, "start")
    assert (await request("GET", uri))["content"] == "c UPDATED 2 Ω\n"
    for path, content in originals.items():
        if path != native:
            assert path.read_bytes() == content
    assert not case.requests()
    (artifacts / "native-file-target-routing.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")


async def test_native_file_uri_events(
    communication_case: CommunicationCase,
) -> None:
    case = communication_case
    external = case.workspace.parent / ("serialization-native-sentinel-" + create_prefixed_id("op"))
    external.mkdir()
    file = case.workspace / "src/serialization.txt"
    file.write_bytes("三个输入的同一正文 Ω\n".encode())
    uri = resource_display_uri(scope="workspace", scope_id=case.backend.workspace_id, kind="files",
        tail_segments=("root", "src", file.name))
    records = []
    gateway_database = case.workspace.parent / "boxteam-home/state/gateway/gateway.sqlite"

    def registry():
        with closing(sqlite3.connect(gateway_database.as_uri() + "?mode=ro", uri=True)) as connection:
            return connection.execute("SELECT * FROM gateway_workspace_registry ORDER BY workspace_id").fetchall()

    registered = registry()

    def assert_no_native_paths(text):
        # JSON 会转义 Windows 反斜杠；归一分隔符后再核对，避免漏掉已序列化的真实路径。
        normalized = re.sub(r"[\\/]+", "/", text).casefold()
        for native in (case.workspace, external):
            assert re.sub(r"[\\/]+", "/", str(native)).casefold() not in normalized

    def checked(response, status=200):
        assert response.status_code == status, response.text
        assert_no_native_paths(response.text)
        body = response.json()
        assert response.headers["X-Request-ID"] == body["request_id"]
        records.append(body)
        return body.get("data")

    async def wait_operation(receipt):
        async with asyncio.timeout(10):
            while True:
                terminal, = checked(await case.client.get("/api/v1/workspace/files/operations", params={
                    "operation_id": receipt["operation_id"], "expected_workspace_id": case.backend.workspace_id,
                }))["operations"]
                if terminal["state"] in {"committed", "rejected", "cancelled", "dependency_failed"}:
                    return terminal
                await asyncio.sleep(0.02)

    roots = checked(await case.client.get("/api/v1/workspace/files/roots"))
    assert roots["workspace_uri"] == uri.rsplit("/src/", 1)[0]
    assert parse_resource_uri(roots["filesystem_uri"]).scope == "gateway"
    references = checked(await case.client.post("/api/v1/workspace/files/references", json={
        "paths": ["src/" + file.name, str(file), uri, str(external)],
    }))["uris"]
    assert references[:3] == [uri] * 3
    external_uri = references[3]
    assert parse_resource_uri(external_uri).scope_id == case.gateway_id
    values = ("src/" + file.name, str(file), uri)
    for value in values:
        content = checked(await case.client.get("/api/v1/workspace/files/content", params={"path": value}))
        assert (content["path"], content["content"]) == (uri, file.read_bytes().decode("utf-8"))
    revision = content["revision"]
    for index, value in enumerate(values):
        saved_text = f"三种输入实际保存 {index} Ω\n"
        saved = checked(await case.client.put("/api/v1/workspace/files/content", params={"path": value}, json={
            "content": saved_text, "expected_revision": revision,
        }))
        assert (saved["path"], saved["content"]) == (uri, saved_text)
        assert file.read_bytes() == saved_text.encode("utf-8")
        revision = saved["revision"]
        for other in values:
            reread = checked(await case.client.get("/api/v1/workspace/files/content", params={"path": other}))
            assert (reread["path"], reread["content"], reread["revision"]) == (uri, saved_text, revision)
    if file.drive:
        checked(await case.client.get("/api/v1/workspace/files/content", params={
            "path": file.drive + "serialization.txt",
        }), status=400)
    directory = checked(await case.client.get("/api/v1/workspace/files", params={"path": str(external)}))
    assert directory["path"] == directory["directory_key"]["path"] == external_uri
    created_name = "真实创建 Ω.txt"
    receipt = checked(await case.client.post("/api/v1/workspace/files/entries", params={"path": external_uri}, json={
        "name": created_name, "kind": "file", "intent": {
            "client_operation_id": create_prefixed_id("op"), "client_sequence": 1,
            "directory_key": directory["directory_key"], "expected_directory_revision": directory["directory_revision"],
        },
    }), status=202)
    terminal = await wait_operation(receipt)
    assert (terminal["state"], terminal["effect"]) == ("committed", "complete")
    assert (external / created_name).is_file() and not (external / ".boxteam").exists()
    async with asyncio.timeout(10):
        async with case.client.stream("POST", "/api/v1/workspace/files/events", json={
            "paths": [external_uri], "after_event_id": 0,
        }) as response:
            assert response.status_code == 200
            async for line in response.aiter_lines():
                if line.startswith("data: "):
                    event = json.loads(line[6:])
                    assert_no_native_paths(line)
                    assert event["event_id"] > 0
                    snapshot, = event["snapshots"]
                    assert snapshot["path"] == external_uri
                    assert [node["name"] for node in snapshot["items"]] == [created_name]
                    assert snapshot["directory_revision"] == terminal["directories"][0]["directory_revision"]
                    records.append(event)
                    break
            else:
                raise AssertionError("文件事件流未返回已持久化的创建结果")
    external_file = external / created_name
    external_file_uri, = checked(await case.client.post("/api/v1/workspace/files/references", json={
        "paths": [str(external_file)],
    }))["uris"]
    external_read = checked(await case.client.get("/api/v1/workspace/files/content", params={"path": external_file_uri}))
    for index, value in enumerate((str(external_file), external_file_uri)):
        text = f"工作区外真实保存 {index} Ω\n"
        saved = checked(await case.client.put("/api/v1/workspace/files/content", params={"path": value}, json={
            "content": text, "expected_revision": external_read["revision"],
        }))
        assert saved["path"] == external_file_uri and saved["content"] == text
        assert external_file.read_bytes() == text.encode("utf-8")
        external_read = checked(await case.client.get("/api/v1/workspace/files/content", params={"path": external_file_uri}))
        assert (external_read["content"], external_read["revision"]) == (text, saved["revision"])
    directory = checked(await case.client.get("/api/v1/workspace/files", params={"path": external_uri}))
    upload_intent = {"client_operation_id": create_prefixed_id("op"), "client_sequence": 2,
        "directory_key": directory["directory_key"], "expected_directory_revision": directory["directory_revision"]}
    uploaded = checked(await case.client.post("/api/v1/workspace/files/upload", params={"path": external_uri},
        data={"relative_paths": "上传 Ω.txt", "intent": json.dumps(upload_intent)},
        files={"files": ("upload.txt", b"uploaded original bytes\n", "text/plain")}), status=202)
    upload_terminal = await wait_operation(uploaded)
    assert (upload_terminal["state"], upload_terminal["effect"]) == ("committed", "complete")
    assert (external / "上传 Ω.txt").read_bytes() == b"uploaded original bytes\n"
    # terminal 先发布，暂存清理随后执行；有界等待真实清理，不把终态当作清理完成。
    async with asyncio.timeout(10):
        while list((case.workspace / ".boxteam/navigation/workspace-files-staging").iterdir()):
            status = checked(await case.client.get("/api/v1/workspace/files/operations", params={
                "operation_id": uploaded["operation_id"], "expected_workspace_id": case.backend.workspace_id,
            }))
            assert status["worker_state"] != "failed", status
            await asyncio.sleep(0.02)
    checked(await case.client.get("/api/v1/workspace/files/content", params={"path": "missing-native-sentinel"}), status=404)
    checked(await case.client.get("/api/v1/workspace/files", params={
        "path": uri.replace(case.backend.workspace_id, "different-workspace"),
    }), status=400)
    database = case.workspace / ".boxteam/navigation/workspace-files.sqlite"
    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as connection:
        dump = "\n".join(connection.iterdump())
    assert_no_native_paths(dump)
    assert registry() == registered
    assert not list(case.replay.artifacts.glob("*.json"))
    with case.replay.control._condition:
        assert not any(key.startswith("model_call/") for key in case.replay.control._counters)
    case.record("file-uri-serialization", {"responses": records, "terminal": terminal, "upload_terminal": upload_terminal})
