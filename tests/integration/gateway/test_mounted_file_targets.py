"""真实共享 Backend 的文件目标固定，原生正文不参与资源寻址。"""

from __future__ import annotations

import asyncio
import json

from app.core.resource_uri import parse_resource_uri
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
    gateway_ids = (primary, secondary)
    headers = [{"X-BoxTeam-Workspace-Id": identity} for identity in gateway_ids]
    workspaces = await asyncio.gather(*(
        case.data("GET", "/api/v1/workspace", headers=selection)
        for selection in headers
    ))
    assert len({workspace["runtime"]["pid"] for workspace in workspaces}) == 1
    assert len({workspace["workspace_id"] for workspace in workspaces}) == 2
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
        selected = await case.data("POST", "/api/v1/resources/navigation",
                                   headers=headers[1 - index], json={"uri": uri, "operation": "read"})
        assert selected["node"]["uri"] == uri and selected["content"] == contents[index]
        rejected = await case.client.get("/api/v1/workspace/files/content", headers=headers[1 - index],
                                         params={"path": uri})
        assert rejected.status_code == 400, rejected.text
        assert rejected.headers["X-Request-ID"] == rejected.json()["request_id"]
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
    assert listed["active_workspace_id"] == secondary
    assert {item["workspace_id"] for item in listed["items"]} == set(gateway_ids)
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
        evidence.append({"root": root, "first": page, "following": following, "read": read})
    case.context.write_result("passed", {"fuzzy_queries": evidence,
        "scope": "真实Gateway、共享Backend双挂载与HTTP；没有Quick Access UI、SSH或模型执行。"})
