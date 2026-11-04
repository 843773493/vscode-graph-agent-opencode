"""真实共享 Backend 的文件目标固定，原生正文不参与资源寻址。"""

from __future__ import annotations

import asyncio
import json

from app.core.resource_uri import parse_resource_uri
from app.core.workspace_identity import read_workspace_id
from tests.integration.test_shared_managed_backend_lifecycle import (
    SharedBackendCase,
    _join_group,
)
from tests.integration.test_shared_managed_backend_lifecycle import (
    shared_backend_case as shared_backend_case,  # noqa: PLC0414
)


async def test_three_file_inputs_keep_mount_owner_and_external_native_target(
    shared_backend_case: SharedBackendCase,
) -> None:
    case = shared_backend_case
    primary, secondary, joined = await _join_group(case)
    workspace_ids = (primary, secondary)
    assert workspace_ids == tuple(read_workspace_id(root) for root in case.roots)
    headers = [{"X-BoxTeam-Workspace-Id": identity} for identity in workspace_ids]
    workspaces = await asyncio.gather(*(
        case.data("GET", "/api/v1/workspace", headers=selection)
        for selection in headers
    ))
    assert len({workspace["runtime"]["pid"] for workspace in workspaces}) == 1
    assert tuple(workspace["workspace_id"] for workspace in workspaces) == workspace_ids
    for identity in workspace_ids:
        explicit = await case.data("GET", f"/api/v1/workspaces/{identity}/workspace")
        assert explicit["workspace_id"] == identity
        assert explicit["runtime"]["pid"] == workspaces[0]["runtime"]["pid"]
    await case.data("POST", f"/api/gateway/workspaces/{secondary}/activate")

    name = "原生路径与 URI.json"
    targets = [root / name for root in case.roots]
    contents = [json.dumps({
        "owner": index,
        "log_path": str(root / "build.log"),
        "other_machine_display": r"C:\Users\alice\project\main.js",
        "other_workspace_display": str(case.roots[1 - index] / name),
    }, ensure_ascii=False) for index, root in enumerate(case.roots)]
    for target, content in zip(targets, contents, strict=True):
        target.write_text(content, encoding="utf-8")

    references = await asyncio.gather(*(
        case.data("POST", "/api/v1/workspace/files/references", headers=selection,
                  json={"paths": [name, str(target)]})
        for selection, target in zip(headers, targets, strict=True)
    ))
    uris = [reference["uris"][0] for reference in references]
    for reference, uri, workspace in zip(references, uris, workspaces, strict=True):
        assert reference["uris"] == [uri, uri]
        address = parse_resource_uri(uri)
        assert address.scope == "workspace" and address.scope_id == workspace["workspace_id"]

    async def read_and_update(index: int) -> None:
        for step, value in enumerate((name, str(targets[index]), uris[index])):
            current = await case.data("GET", "/api/v1/workspace/files/content",
                                      headers=headers[index], params={"path": value})
            assert current["path"] == uris[index] and current["content"] == contents[index]
            assert parse_resource_uri(current["root_path"]).scope_id == workspaces[index]["workspace_id"]
            contents[index] += f"\n更新 {index}/{step}"
            updated = await case.data("PUT", "/api/v1/workspace/files/content",
                                      headers=headers[index], params={"path": value}, json={
                                          "content": contents[index], "expected_revision": current["revision"],
                                      })
            assert updated["path"] == uris[index] and updated["content"] == contents[index]
            assert updated["revision"] != current["revision"]
            assert targets[index].read_text(encoding="utf-8") == contents[index]

    # 两个挂载同时读写同名文件；工作区选择只由各自请求与资源 owner 决定。
    await asyncio.gather(read_and_update(0), read_and_update(1))
    for index, uri in enumerate(uris):
        explicit = await case.data("GET", f"/api/v1/workspaces/{workspace_ids[index]}/workspace/files/content",
                                   params={"path": uri})
        assert explicit["path"] == uri and explicit["content"] == contents[index]
        selected = await case.data("POST", "/api/v1/resources/navigation",
                                   headers=headers[1 - index], json={"uri": uri, "operation": "read"})
        assert selected["node"]["uri"] == uri and selected["content"] == contents[index]
        rejected = await case.client.get("/api/v1/workspace/files/content", headers=headers[1 - index],
                                         params={"path": uri})
        assert rejected.status_code == 400, rejected.text
        assert rejected.headers["X-Request-ID"] == rejected.json()["request_id"]
        conflict = await case.client.get(
            f"/api/v1/workspaces/{workspace_ids[index]}/workspace/files/content",
            headers={**headers[1 - index], "X-Request-ID": f"canonical-file-conflict-{index}"}, params={"path": uri},
        )
        assert conflict.status_code == 400, conflict.text
        assert conflict.json()["detail"] == "路径与请求头的工作区 UUID 不一致"
        assert conflict.headers["X-Request-ID"] == conflict.json()["request_id"] == f"canonical-file-conflict-{index}"
        assert targets[index].read_text(encoding="utf-8") == contents[index]

    # 未注册目录仍由原生 Gateway 唯一绑定；绝对路径不能改投工作区内同名位置。
    external = case.context.workspace_root / "unregistered" / "outside.txt"
    external.parent.mkdir()
    external.write_text("工作区外原件\n", encoding="utf-8")
    external_uris = []
    for selection in headers:
        reference = await case.data("POST", "/api/v1/workspace/files/references", headers=selection,
                                    json={"paths": [str(external)]})
        external_uris.append(reference["uris"][0])
    assert external_uris[0] == external_uris[1]
    external_uri = external_uris[0]
    assert parse_resource_uri(external_uri).scope == "gateway"
    for index, value in enumerate((str(external), external_uri)):
        selection = headers[index]
        original = await case.data("GET", "/api/v1/workspace/files/content", headers=selection,
                                   params={"path": value})
        assert original["path"] == external_uri and original["content"] == external.read_text(encoding="utf-8")
        changed = original["content"] + f"目标机器更新 {index}\n"
        updated = await case.data("PUT", "/api/v1/workspace/files/content", headers=selection,
                                  params={"path": value}, json={
                                      "content": changed, "expected_revision": original["revision"],
                                  })
        assert updated["path"] == external_uri and external.read_text(encoding="utf-8") == changed
    listed = await case.data("GET", "/api/gateway/workspaces")
    assert listed["active_workspace"] == {"gateway_id": joined["gateway_id"], "workspace_id": secondary}
    assert {(item["gateway_id"], item["workspace_id"]) for item in listed["items"]} == {
        (joined["gateway_id"], identity) for identity in workspace_ids
    }
    assert {item["backend_generation"] for item in listed["items"]} == {joined["backend_generation"]}
    assert [target.read_text(encoding="utf-8") for target in targets] == contents


async def test_fuzzy_files_query_exact_mount_with_bounded_continuation(
    shared_backend_case: SharedBackendCase,
) -> None:
    case = shared_backend_case
    primary, secondary, _ = await _join_group(case)
    selections = [{"X-BoxTeam-Workspace-Id": route} for route in (primary, secondary)]
    roots = []
    relative_names = ("z-source/MemoryComponent.ts", "z-source/MyController.ts")
    for index, (workspace, selection) in enumerate(zip(case.roots, selections, strict=True)):
        directory = workspace / "fuzzy-query"
        (directory / "z-source").mkdir(parents=True)
        for item in range(120):
            (directory / f"a-unrelated-{item:03}.txt").touch()
        for name in relative_names:
            (directory / name).write_text(f"mount-{index} {name}", encoding="utf-8")
        (directory / "多行😀.txt").write_bytes(f"🧪 first NEEDLE\r\nsecond Ω owner-{index}\r\n".encode())
        references = await case.data("POST", "/api/v1/workspace/files/references", headers=selection,
                                     json={"paths": [str(directory)]})
        roots.append(references["uris"][0])
    assert roots[0] != roots[1]
    await case.data("POST", f"/api/gateway/workspaces/{secondary}/activate")
    evidence = []
    for index, (root, selection) in enumerate(zip(roots, selections, strict=True)):
        payload = {"uri": root, "operation": "search", "search_mode": "fuzzy", "query": "MCT", "limit": 1}
        page = await case.data("POST", "/api/v1/resources/navigation", headers=selection, json=payload)
        assert page["truncated"] and page["truncation_reason"] == "entry_limit" and page["next_cursor"]
        first_uri = page["entries"][0]["uri"]
        assert first_uri.startswith(root + "/") and not page["matches"]
        following = await case.data("POST", "/api/v1/resources/navigation", headers=selection,
                                    json={**payload, "cursor": page["next_cursor"]})
        found = [first_uri, *(node["uri"] for node in following["entries"])]
        assert not following["truncated"] and following["next_cursor"] is None
        assert set(found) == {root + "/" + name for name in relative_names}
        read = await case.data("POST", "/api/v1/resources/navigation", headers=selection,
                               json={"uri": first_uri, "operation": "read"})
        assert read["content"].startswith(f"mount-{index} ")
        for changed, reason in (({"search_mode": "path"}, "invalid_cursor"), ({"scan_limit": 3}, "scan_limit")):
            rejected = await case.client.post("/api/v1/resources/navigation", headers=selection,
                json={**payload, **({"cursor": page["next_cursor"]} if reason == "invalid_cursor" else {}), **changed})
            assert rejected.status_code == 400, rejected.text
            assert rejected.headers["X-Request-ID"] == rejected.json()["request_id"]
            assert rejected.json()["detail"]["reason"] == reason
        content = await case.data("POST", "/api/v1/resources/navigation", headers=selection, json={
            "uri": root, "operation": "search", "search_mode": "content", "query": r"needle\r?\nsecond Ω",
            "search_options": {"case_mode": "insensitive", "is_regex": True, "is_multiline": True,
                               "all_matches": True, "include_globs": ["多行*.txt"], "exclude_globs": ["a-**"]},
        })
        match, = content["matches"]
        assert (match["line"], match["column"], match["end_line"], match["end_column"]) == (1, 9, 2, 9)
        assert (match["utf16_column"], match["utf16_end_column"], match["snippet_start_utf16_column"]) == (10, 9, 1)
        assert match["uri"].startswith(root + "/") and match["text"] == "🧪 first NEEDLE"
        original = await case.data("POST", "/api/v1/resources/navigation", headers=selection,
                                   json={"uri": match["uri"], "operation": "read"})
        assert original["content"].endswith(f"owner-{index}\r\n")
        unsupported = await case.client.post("/api/v1/resources/navigation", headers=selection, json={
            "uri": root, "operation": "search", "query": "hit", "search_options": {"ignore_mode": "respect"},
        })
        assert unsupported.status_code == 400, unsupported.text
        assert unsupported.headers["X-Request-ID"] == unsupported.json()["request_id"]
        assert unsupported.json()["detail"]["reason"] == "unsupported_search_option"
        evidence.append({"root": root, "first": page, "following": following, "read": read,
                         "content": content, "original": original, "unsupported": unsupported.json()})
    case.context.write_result("passed", {"fuzzy_queries": evidence,
        "scope": "真实Gateway、共享Backend双挂载与HTTP；没有Quick Access UI、SSH或模型执行。"})
