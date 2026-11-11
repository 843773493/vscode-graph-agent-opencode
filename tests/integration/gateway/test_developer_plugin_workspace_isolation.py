"""正式 Terminal／Browser 在同一 Gateway 的双工作区隔离和卸载闭环。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from urllib.parse import urlencode

import commentjson
import httpx
import psutil
import pytest
from websockets.sync.client import connect

from tests.integration.gateway.test_browser_plugin_lifecycle import _receive_message
from tests.integration.gateway.test_service_plugin_lifecycle import _same_process
from tests.integration.gateway.test_service_plugin_workspaces import (
    workspace_plugin_case as workspace_plugin_case,  # noqa: PLC0414 - 原真实 Gateway／双工作区装具
)
from tests.integration.gateway.test_terminal_plugin_lifecycle import _receive_output
from tests.support.processes.gateway import LOCAL_TOKEN_HEADERS
from tests.support.terminal_plugin import (
    copy_prepared_developer_tools_package,
    enable_developer_tools_plugins,
)

PACKAGE = "boxteam.developer-tools"
VERSION = "0.1.0"


async def test_developer_plugins_isolate_resources_and_release_package_references(
    workspace_plugin_case, request: pytest.FixtureRequest, prepared_developer_tools_package: Path,
) -> None:
    sync_client, context, workspaces = workspace_plugin_case
    home = context.boxteam_home_for_node(request.node.nodeid)
    package_root = copy_prepared_developer_tools_package(prepared_developer_tools_package, home)
    async with httpx.AsyncClient(base_url=str(sync_client.base_url), headers=LOCAL_TOKEN_HEADERS,
        trust_env=False, timeout=90) as client:
        gateway = (await client.get("/api/gateway/health")).raise_for_status().json()["data"]["gateway_id"]
        targets: list[str] = []
        slots: list[dict] = []
        sessions: list[dict] = []
        resources: list[dict[str, dict]] = []
        evidence: list[dict] = []
        for index, workspace in enumerate(workspaces):
            name = f"Resource isolation {index}"
            response = await client.post("/api/gateway/managed-workspaces", json={
                "root_path": str(workspace), "name": name, "create_directory": False,
            })
            response.raise_for_status()
            target = next(item["workspace_id"] for item in response.json()["data"]["items"] if item["name"] == name)
            targets.append(target)
            slots.append(await enable_developer_tools_plugins(client, gateway_id=gateway,
                workspace_id=target, plugin_ids=("terminal", "browser")))
            headers = {"X-BoxTeam-Workspace-Id": target}
            session = (await client.post("/api/v1/sessions", headers=headers,
                json={"title": "同名资源隔离"})).raise_for_status().json()["data"]
            sessions.append(session)
            common = {"session_id": session["session_id"], "thread_id": session["thread_id"],
                "retention_scope": "workspace", "title": "相同显示名称"}
            terminal = (await client.post("/api/v1/resources/terminals", headers=headers,
                json={**common, "cwd_relative": ".", "cols": 100, "rows": 30})).raise_for_status().json()["data"]
            browser = (await client.post("/api/v1/resources/browsers", headers=headers,
                json={**common, "url": f"data:text/html,<title>isolation-{index}</title>"})).raise_for_status().json()["data"]
            resources.append({"terminal": terminal, "browser": browser})
            for kind, resource in resources[-1].items():
                assert resource["workspace_id"] == target
                grant = (await client.post(f"/api/v1/resources/{resource['resource_id']}/transport-grant",
                    headers=headers, json={"expected_revision": resource["owner_revision"]})).raise_for_status().json()["data"]
                instance = slots[-1][kind]["instance"]
                assert grant["service"]["instance_id"] == instance["instance_id"]
                assert grant["service"]["workspace_id"] == target
                query = urlencode({"workspace_id": target, "package_version": VERSION,
                    "target_gateway_id": gateway, "token": LOCAL_TOKEN_HEADERS["X-Local-Token"]})
                url = (str(client.base_url).replace("http://", "ws://").rstrip("/")
                    + f"/api/gateway/service-plugins/{PACKAGE}/{kind}/instances/{instance['instance_id']}"
                    + f"/services/socket/{kind}?{query}")
                with connect(url, open_timeout=10, close_timeout=3) as socket:
                    socket.send(json.dumps({"type": "attach", f"{kind}Id": resource["resource_id"],
                        "grantToken": grant["grant_token"]}))
                    attached = _receive_message(socket, "attached")
                    assert attached[f"{kind}Id"] == resource["resource_id"]
                    if kind == "terminal":
                        socket.send(json.dumps({"type": "input", "data": f"printf 'isolation-%s\\n' '{index}'\n"}))
                        output = _receive_output(socket, f"isolation-{index}")
                        assert output["terminalId"] == resource["resource_id"]
                    else:
                        assert resource["metadata"]["pages"][0]["title"] == f"isolation-{index}"
                evidence.append({"workspace": target, "kind": kind, "instance": instance,
                    "resource": resource, "attached": attached})

        processes = [psutil.Process(item[kind]["instance"]["process_id"]) for item in slots for kind in ("terminal", "browser")]
        assert len({process.pid for process in processes}) == 4
        for kind in ("terminal", "browser"):
            assert resources[0][kind]["resource_id"] != resources[1][kind]["resource_id"]
            for index in (0, 1):
                foreign = resources[1 - index][kind]["resource_id"]
                denied = await client.get(f"/api/v1/resources/{foreign}", headers={"X-BoxTeam-Workspace-Id": targets[index]})
                assert denied.status_code == 404, denied.text
                instance = slots[index][kind]["instance"]["instance_id"]
                wrong_route = await client.get(f"/api/gateway/service-plugins/{PACKAGE}/{kind}/instances/{instance}/services/http/api/{kind}s",
                    params={"workspace_id": targets[1 - index], "package_version": VERSION},
                    headers={"X-BoxTeam-Resource-Owner": f"workspace-{kind}"})
                assert wrong_route.status_code == 409, wrong_route.text

        async def save_entries(target: str, plugin_ids: tuple[str, ...], key: str) -> None:
            path = f"/api/v1/workspaces/{target}/config/sources/workspace_root_mutable_override"
            headers = {"X-BoxTeam-Workspace-Id": target}
            document = (await client.get(path, headers=headers)).raise_for_status().json()["data"]
            config = commentjson.loads(document["jsonc"])
            config["plugins"]["runtime"]["entries"] = [{"package_id": PACKAGE, "package_version": VERSION,
                "plugin_id": plugin_id, "enabled": True} for plugin_id in plugin_ids]
            response = await client.put(path, headers=headers, json={"gateway_id": gateway, "workspace_id": target,
                "source_key": "workspace_root_mutable_override", "jsonc": json.dumps(config),
                "base_file_digest": document["file_digest"], "base_layer_revision": document["layer_revision"],
                "base_layer_digest": document["layer_digest"], "expected_active_revision": document["active_revision"],
                "expected_active_digest": document["active_digest"], "idempotency_key": key})
            response.raise_for_status()
            assert response.json()["data"]["edit"]["state"] == "active", response.text

        await save_entries(targets[0], ("terminal",), "isolation-close-browser-a")
        status = (await client.post("/api/gateway/service-plugins/status", json={"gateway_id": gateway})).raise_for_status().json()["data"]
        for index, kinds in ((0, ("terminal",)), (1, ("terminal", "browser"))):
            for kind in kinds:
                current, = [slot for slot in status["plugins"] if slot["binding"]["workspace_id"] == targets[index]
                    and slot["binding"]["plugin_id"] == kind]
                assert current["state"] == "ready" and current["instance"] == slots[index][kind]["instance"]
                resource = (await client.get(f"/api/v1/resources/{resources[index][kind]['resource_id']}",
                    headers={"X-BoxTeam-Workspace-Id": targets[index]})).raise_for_status().json()["data"]
                assert resource["status"] == "running"
        uninstall = {"gateway_id": gateway, "package_id": PACKAGE, "package_version": VERSION}
        blocked = await client.post("/api/gateway/service-plugins/uninstall", json=uninstall)
        assert blocked.status_code == 409 and package_root.is_dir(), blocked.text
        for target in targets:
            await save_entries(target, (), f"isolation-unbind-{target}")
        async with asyncio.timeout(10):
            while any(_same_process(process) for process in processes):
                await asyncio.sleep(0.05)
        removed = (await client.post("/api/gateway/service-plugins/uninstall", json=uninstall)).raise_for_status().json()
        assert removed["data"]["uninstalled"] and not package_root.exists()
        for workspace, session in zip(workspaces, sessions, strict=True):
            assert list((workspace / ".boxteam/sessions").rglob(session["session_id"])), "卸载包不能删除原业务会话"
        (context.artifacts_dir / "resource-isolation.json").write_text(json.dumps({
            "gateway_id": gateway, "workspaces": targets, "resources": evidence,
            "after_close_a": status, "uninstall_blocked": blocked.json(), "uninstall": removed,
        }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
