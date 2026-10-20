"""通用导航通过正式 Gateway API、SSH channel 与目标文件 owner 的闭包。"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from contextlib import closing
from uuid import uuid4

from app.core.resource_uri import resource_display_uri
from app.gateway.credentials import FederationCredentialStore
from app.gateway.federation.errors import FEDERATION_TARGET_OWNER_REJECTED
from tests.integration.backend.agents.test_resource_navigation import _tracking_counts
from tests.integration.backend.sessions.test_session_communication_pipeline import (
    _completed,
    _tool,
)
from tests.integration.backend.sessions.test_session_communication_pipeline import (
    communication_triad as communication_triad,  # noqa: PLC0414 - 正式 fixture 按当前测试文件隔离
)
from tests.support.api_helpers import create_session


async def test_navigation_uses_original_star_channels_and_fixed_owner(communication_triad) -> None:
    nodes, model, artifacts = communication_triad
    source, hub, target = nodes["b"], nodes["a"], nodes["c"]
    external = target.workspace.parent / "unregistered-native"
    external.mkdir(exist_ok=True)
    (external / "native.txt").write_text("目标原生目录正文 Ω\n")
    def registry_snapshot():
        with closing(sqlite3.connect(f"file:{target.gateway_root / 'gateway.sqlite'}?mode=ro", uri=True)) as database:
            return database.execute("SELECT * FROM gateway_workspace_registry ORDER BY workspace_id").fetchall()

    registered_before = registry_snapshot()
    references = await target.client.post("/api/v1/workspace/files/references", json={"paths": [str(external)]})
    assert references.status_code == 200, references.text
    native_root = references.json()["data"]["uris"][0]
    assert native_root.startswith(f"boxteam://gateway/{target.gateway_id}/resources/files/")
    child = target.workspace / "src/联邦 文件.v2.md"
    child.write_text("跨 Gateway 正文 Ω\n")
    search_file = target.workspace / "src/联邦 搜索%.txt"
    search_file.write_text("needle-on-target\nneedle-on-target second\n")
    decoy = source.workspace / "src/联邦 搜索%.txt"
    decoy.write_text("SOURCE DECOY\n")
    skill = target.workspace / ".boxteam/skills/navigation/references"
    skill.mkdir(parents=True)
    (skill.parent / "SKILL.md").write_text("---\nname: navigation\ndescription: 导航验证\n---\n正文\n")
    (skill / "远端.md").write_text("工作区技能 child\n")
    assert target.gateway_home is not None
    gateway_skill = target.gateway_home / "skills/navigation/scripts"
    gateway_skill.mkdir(parents=True)
    (gateway_skill.parent / "SKILL.md").write_text("---\nname: navigation\ndescription: 导航验证\n---\n正文\n")
    (gateway_skill / "run.py").write_text("print('远端')\n")
    before = _tracking_counts(target.workspace / ".boxteam")
    evidence = []

    def uri(kind: str, *tail: str, scope: str = "workspace", authority: str | None = target.gateway_id, workspace_id: str = target.workspace_id) -> str:
        return resource_display_uri(scope=scope, scope_id=workspace_id if scope == "workspace" else target.gateway_id, kind=kind, tail_segments=tail, gateway_authority=authority)

    async def navigate(node, address: str, operation: str, **extra: object) -> dict:
        response = await node.client.post("/api/v1/resources/navigation", json={"uri": address, "operation": operation, **extra})
        assert response.status_code == 200, response.text
        assert response.headers["X-Request-ID"] == response.json()["request_id"]
        for member in nodes.values():
            assert str(member.workspace) not in response.text
            assert str(member.gateway_home) not in response.text
        data = response.json()["data"]
        assert data["node"]["uri"] == address
        evidence.append({"source": node.gateway_id, "uri": address, "operation": operation, "data": data})
        return data

    native_uri = native_root.replace("boxteam://", f"boxteam://{target.gateway_id}/")
    native_listing = await navigate(source, native_uri, "list")
    assert len(native_listing["entries"]) == 1
    assert (await navigate(source, native_listing["entries"][0]["uri"], "read"))["content"] == "目标原生目录正文 Ω\n"
    assert (await navigate(target, native_root, "list"))["node"]["uri"] == native_root
    assert str(external) not in json.dumps(native_listing, ensure_ascii=False)
    assert not (external / ".boxteam").exists()
    assert registry_snapshot() == registered_before

    root = uri("files", "root", "src")
    # hub 一跳直达与 spoke 经唯一 hub 的两跳返回相同可继续访问的目标 URI。
    direct = await navigate(hub, root, "list")
    relayed = await navigate(source, root, "list")
    assert direct == relayed
    file_uri = uri("files", "root", "src", "联邦 搜索%.txt")
    searched = await navigate(source, file_uri, "search", search_mode="content", query="needle-on-target", limit=1)
    match, = searched["matches"]
    assert match["uri"] == searched["entries"][0]["uri"] == file_uri
    assert (match["line"], match["column"], match["text"]) == (1, 1, "needle-on-target")
    assert searched["truncated"] and searched["next_cursor"]
    following_match = await navigate(source, file_uri, "search", search_mode="content", query="needle-on-target", limit=1, cursor=searched["next_cursor"])
    assert following_match["matches"][0]["line"] == 2 and not following_match["truncated"]
    assert (await navigate(source, file_uri, "read"))["content"] == search_file.read_text()
    assert decoy.read_text() == "SOURCE DECOY\n"
    first = await navigate(source, root, "list", limit=1)
    assert first["truncated"] and first["next_cursor"]
    following = await navigate(source, root, "list", limit=1, cursor=first["next_cursor"])
    assert following["entries"][0]["uri"] != first["entries"][0]["uri"]
    assert following["entries"][0]["uri"].startswith(f"boxteam://{target.gateway_id}/")
    for address in (root, uri("skills", "navigation", scope="gateway")):
        limited = await source.client.post("/api/v1/resources/navigation", json={
            "uri": address, "operation": "list", "scan_limit": 1,
        })
        assert limited.status_code == 409, limited.text
        error = limited.json()["detail"]
        assert error["code"] == FEDERATION_TARGET_OWNER_REJECTED
        assert error["detail"]["owner"] == "resource_file_operations"
        assert error["detail"]["reason"] == "scan_limit"
    # canonical remote route 也必须使用同一 read grant；不能依赖对端 HTTP 凭据直读。
    local_root = uri("files", "root", "src", authority=None)
    canonical_path = f"/api/v1/workspaces/{target.workspace_id}/resources/navigation"
    canonical = await hub.client.post(canonical_path, json={"uri": local_root, "operation": "list"})
    assert canonical.status_code == 200, canonical.text
    assert canonical.json()["data"] == direct
    assert canonical.headers["X-Request-ID"] == canonical.json()["request_id"]
    credential = FederationCredentialStore(storage_path=hub.gateway_root / "credentials/federation.json").get("rgw_c3_c")
    for path in ("/api/v1/resources/navigation", canonical_path):
        rejected = await target.client.post(path, json={"uri": local_root, "operation": "list"}, headers={
            "X-Local-Token": "", "X-BoxTeam-Federation-Token": credential.token,
        })
        assert rejected.status_code == 400, rejected.text
        assert rejected.json()["detail"] == "联邦资源导航必须通过原 read grant relay"
    entry = next(item for item in relayed["entries"] if item["uri"].endswith("%E8%81%94%E9%82%A6%20%E6%96%87%E4%BB%B6.v2.md"))
    assert entry["uri"].startswith(f"boxteam://{target.gateway_id}/")
    assert (await navigate(source, entry["uri"], "read"))["content"] == "跨 Gateway 正文 Ω\n"
    found = await navigate(source, root, "search", query="联邦", limit=1)
    assert len(found["entries"]) == 1 and found["truncated"]
    remaining = await navigate(source, root, "search", query="联邦", limit=1, cursor=found["next_cursor"])
    assert len(remaining["entries"]) == 1 and not remaining["truncated"]
    assert {found["entries"][0]["uri"], remaining["entries"][0]["uri"]} == {entry["uri"], file_uri}
    assert (await navigate(source, entry["uri"], "read", max_bytes=3))["truncated"] is True
    for scope, segments, expected in (
        ("workspace", ("navigation", "references", "远端.md"), "工作区技能 child\n"),
        ("gateway", ("navigation", "scripts", "run.py"), "print('远端')\n"),
    ):
        address = uri("skills", *segments, scope=scope)
        assert (await navigate(source, address, "read"))["content"] == expected
    # 缺省与本机 authority 同解，且不把 active workspace 当 URI 的补全来源。
    local = uri("files", "root", "src", child.name, authority=None)
    explicit = uri("files", "root", "src", child.name)
    local_data = await navigate(target, local, "read")
    same = await target.client.post("/api/v1/resources/navigation", json={"uri": explicit, "operation": "read"})
    assert same.status_code == 200 and same.json()["data"] == local_data
    missing_codes = []
    for address, code in (
        (uri("files", "root", "missing.txt"), "target_not_resolvable"),
        (uri("files", "root", workspace_id=str(uuid4())), "target_not_resolvable"),
        (uri("files", "root", ".boxteam", "workspace-identity.json"), "target_not_resolvable"),
        (uri("files", "root", authority="gateway_unknown"), "federation-unknown-peer"),
    ):
        response = await source.client.post("/api/v1/resources/navigation", json={"uri": address, "operation": "read"})
        assert response.status_code == 409, response.text
        assert response.json()["detail"]["code"] == code
        assert response.headers["X-Request-ID"] == response.json()["request_id"]
        assert str(target.workspace) not in response.text
        missing_codes.append(response.json()["detail"])
    assert missing_codes[0] == missing_codes[1] == missing_codes[2]
    assert _tracking_counts(target.workspace / ".boxteam") == before
    assert not model.requests
    assert {(peer, direction) for peer, direction in source.channels()} == {(hub.gateway_id, "inbound")}
    assert {(peer, direction) for peer, direction in target.channels()} == {(hub.gateway_id, "inbound")}
    (artifacts / "navigation-federation.json").write_text(json.dumps({"requests": evidence, "rejected": missing_codes}, ensure_ascii=False, indent=2) + "\n")


async def test_model_file_tools_execute_on_fixed_federated_owner(communication_triad) -> None:
    nodes, model, artifacts = communication_triad
    source, target = nodes["b"], nodes["c"]
    (target.workspace / "src/remote-model.txt").write_text("needle TARGET OWNER\n")
    (source.workspace / "src/remote-model.txt").write_text("SOURCE DECOY\n")
    (source.workspace / "src/missing-remote.txt").write_text("LOCAL FALLBACK MUST NOT BE READ\n")
    root = resource_display_uri(scope="workspace", scope_id=target.workspace_id, kind="files",
        tail_segments=("root", "src"), gateway_authority=target.gateway_id)
    file_uri, missing_uri = root + "/remote-model.txt", root + "/missing-remote.txt"
    session = await create_session(source.client, "模型远端文件导航")
    before = _tracking_counts(target.workspace / ".boxteam")
    tool_results = []

    def reply(payload):
        messages = payload["messages"]
        if not any(message["role"] == "user" and "federated-file-trigger" in str(message["content"]) for message in messages):
            return {"content": "远端文件导航"}
        results = [message for message in messages if message["role"] == "tool"]
        if not results:
            return _tool("ls", {"path": root}, "call_remote_ls")
        listed = json.loads(results[0]["content"])
        assert file_uri in {entry["uri"] for entry in listed["entries"]}
        if len(results) == 1:
            return _tool("glob", {"path": root, "pattern": "remote-model.txt"}, "call_remote_glob")
        assert [entry["uri"] for entry in json.loads(results[1]["content"])["entries"]] == [file_uri]
        if len(results) == 2:
            return _tool("grep", {"path": file_uri, "pattern": "needle", "output_mode": "content"}, "call_remote_grep")
        assert file_uri in results[2]["content"] and "TARGET OWNER" in results[2]["content"]
        if len(results) == 3:
            return _tool("read_file", {"path": file_uri}, "call_remote_read")
        assert "1\tneedle TARGET OWNER" in results[3]["content"]
        assert "sha256:" + hashlib.sha256(b"needle TARGET OWNER\n").hexdigest() in results[3]["content"]
        if len(results) == 4:
            return _tool("read_file", {"path": missing_uri}, "call_remote_missing")
        assert json.loads(results[4]["content"])["code"] == "target_not_resolvable"
        assert "SOURCE DECOY" not in json.dumps(results) and "LOCAL FALLBACK" not in json.dumps(results)
        tool_results[:] = results
        return {"content": "远端文件工具完成"}

    model.response_callback = reply
    accepted = await source.client.post(f"/api/v1/sessions/{session}/messages", json={
        "message": {"content": "federated-file-trigger"}, "run": {"mode": "single_agent", "agent_id": "default"},
    }, headers={"X-Request-ID": "req-model-federated-files"})
    assert accepted.status_code == 200, accepted.text
    await _completed(source.client, accepted.json()["data"]["job_id"])
    assert len(tool_results) == 5
    assert (source.workspace / "src/remote-model.txt").read_text() == "SOURCE DECOY\n"
    assert _tracking_counts(target.workspace / ".boxteam") == before
    wire = json.dumps(model.requests, ensure_ascii=False)
    assert all(str(node.workspace) not in wire for node in nodes.values())
    (artifacts / "model-federated-files.json").write_text(json.dumps({"results": tool_results,
        "target_tracking_before": before, "target_tracking_after": _tracking_counts(target.workspace / ".boxteam")},
        ensure_ascii=False, indent=2) + "\n")


async def test_model_conditional_patch_preserves_file_operations_and_changesets(communication_triad):
    nodes, model, artifacts = communication_triad
    source = nodes["b"]
    (source.workspace / "src/patch-source.txt").write_bytes(b"old\n")
    (source.workspace / "src/patch-delete.txt").write_bytes(b"delete\n")
    root = resource_display_uri(scope="workspace", scope_id=source.workspace_id, kind="files", tail_segments=("root", "src"))
    session = await create_session(source.client, "模型条件补丁")
    receipt = None
    patch = "*** Begin Patch\n*** Update File: patch-source.txt\n*** Move to: patch-destination.txt\n@@\n-old\n+new\n*** Delete File: patch-delete.txt\n*** Add File: patch-created.txt\n+created\n*** End Patch"

    def reply(payload):
        nonlocal receipt
        messages = payload["messages"]
        if not any(message["role"] == "user" and "conditional-patch-trigger" in str(message["content"]) for message in messages):
            return {"content": "条件补丁"}
        results = [message for message in messages if message["role"] == "tool"]
        if not results:
            return _tool("read_file", {"path": root + "/patch-source.txt"}, "call_patch_read_source")
        if len(results) == 1:
            return _tool("read_file", {"path": root + "/patch-delete.txt"}, "call_patch_read_delete")
        if len(results) == 2:
            versions = {name: re.search(r"sha256:[0-9a-f]{64}", message["content"]).group()
                        for name, message in zip(("patch-source.txt", "patch-delete.txt"), results, strict=True)}
            assert versions == {"patch-source.txt": "sha256:" + hashlib.sha256(b"old\n").hexdigest(),
                                "patch-delete.txt": "sha256:" + hashlib.sha256(b"delete\n").hexdigest()}
            versions.update({"patch-destination.txt": "absent", "patch-created.txt": "absent"})
            return _tool("apply_patch", {"input": patch, "explanation": "新增、删除和同根移动", "target": root,
                                        "expected_versions": versions}, "call_conditional_patch")
        receipt = json.loads(results[2]["content"])
        assert receipt["status"] == "success" and receipt["effect"] == "completed", receipt
        assert all(item["state"] == "published" for item in receipt["files"])
        assert receipt["target"] == root
        if len(results) == 3:
            assert (source.workspace / "src/patch-created.txt").read_bytes() == b"created"
            return _tool("read_file", {"path": root + "/patch-created.txt"}, "call_edit_read")
        if len(results) == 4:
            version = re.search(r"sha256:[0-9a-f]{64}", results[3]["content"]).group()
            assert version == "sha256:" + hashlib.sha256(b"created").hexdigest()
            return _tool("edit_file", {"file_path": root + "/patch-created.txt", "old_string": "created", "new_string": "edited",
                                      "expected_version": version}, "call_conditional_edit")
        edited = json.loads(results[4]["content"])
        assert edited["status"] == "success" and edited["effect"] == "completed", edited
        if len(results) == 5:
            return _tool("write_file", {"file_path": root + "/written.txt", "content": "written\r\n", "expected_version": "absent"}, "call_conditional_write")
        written = json.loads(results[5]["content"])
        assert written["status"] == "success" and written["files"][0]["state"] == "published", written
        return {"content": "已核实条件补丁、精确编辑和创建的统一发布回执"}

    model.response_callback = reply
    accepted = await source.client.post(f"/api/v1/sessions/{session}/messages", json={
        "message": {"content": "conditional-patch-trigger"}, "run": {"mode": "single_agent", "agent_id": "default"},
    }, headers={"X-Request-ID": "req-model-conditional-patch"})
    assert accepted.status_code == 200, accepted.text
    await _completed(source.client, accepted.json()["data"]["job_id"])
    assert receipt is not None
    assert (source.workspace / "src/patch-destination.txt").read_bytes() == b"new\n"
    assert (source.workspace / "src/patch-created.txt").read_bytes() == b"edited"
    assert (source.workspace / "src/written.txt").read_bytes() == b"written\r\n"
    assert not (source.workspace / "src/patch-source.txt").exists() and not (source.workspace / "src/patch-delete.txt").exists()
    response = await source.client.get(f"/api/v1/sessions/{session}/changesets/all")
    assert response.status_code == 200, response.text
    changes = {item["file_path"]: item for item in response.json()["data"]["files"]}
    expected_paths = {root + "/" + name for name in ("patch-source.txt", "patch-destination.txt", "patch-delete.txt", "patch-created.txt", "written.txt")}
    assert set(changes) == expected_paths
    changes = {"/src/" + path.rsplit("/", 1)[1]: value for path, value in changes.items()}
    assert set(changes) == {"/src/patch-source.txt", "/src/patch-destination.txt", "/src/patch-delete.txt", "/src/patch-created.txt", "/src/written.txt"}
    assert changes["/src/patch-source.txt"]["kind"] == changes["/src/patch-delete.txt"]["kind"] == "delete"
    assert changes["/src/patch-destination.txt"]["kind"] == changes["/src/patch-created.txt"]["kind"] == "create"
    assert "-old" in changes["/src/patch-source.txt"]["diff_text"] and "+new" in changes["/src/patch-destination.txt"]["diff_text"]
    assert changes["/src/written.txt"]["kind"] == "create"
    assert "+edited" in changes["/src/patch-created.txt"]["diff_text"]
    assert "+written" in changes["/src/written.txt"]["diff_text"]
    from app.core.path_utils import get_session_path_resolver
    session_path = get_session_path_resolver(source.workspace / ".boxteam/sessions").resolve_session_node(session)
    journals = list((session_path / "changes/text_mutations").glob("*.json"))
    assert len(journals) == 3
    assert all(json.loads(path.read_text())["files"] for path in journals)
    assert not list((source.workspace / ".boxteam/cache/text_mutations").glob("*.json"))
    file_uri = root + "/patch-created.txt"
    review = await source.client.post(f"/api/v1/sessions/{session}/changesets/all/review",
        json={"file_path": file_uri, "reviewed": True})
    assert review.status_code == 200, review.text
    assert review.json()["data"]["file_path"] == file_uri and review.json()["data"]["reviewed"]
    refreshed = await source.client.get(f"/api/v1/sessions/{session}/changesets/all")
    reviewed = next(item for item in refreshed.json()["data"]["files"] if item["file_path"] == file_uri)
    assert reviewed["reviewed"] is True and "+edited" in reviewed["diff_text"]
    wire = json.dumps(model.requests)
    assert all(str(node.workspace) not in wire for node in nodes.values())
    (artifacts / "model-conditional-patch.json").write_text(json.dumps({"receipt": receipt, "changeset": response.json()}, ensure_ascii=False, indent=2) + "\n")
