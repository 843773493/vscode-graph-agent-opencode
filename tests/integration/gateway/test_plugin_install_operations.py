"""真实 Gateway 的自有目录、唯一安装事实与取消／发布竞争。"""

from __future__ import annotations

import hashlib
import io
import json
import threading
import time
import zipfile
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import commentjson
import httpx
import pytest

from tests.harness.python.run_context import TestRunContext
from tests.support.http_server import running_http_server
from tests.support.plugin_package import install_plugin


@pytest.fixture
def plugin_source(request: pytest.FixtureRequest) -> Iterator[dict]:
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    manifest = {"manifest_version": 1, "package_id": "source.proof", "version": "1.0.0",
        "name": "自有来源安装探针", "extension_contract_version": 1,
        "plugins": [{"id": "declaration", "name": "声明探针", "instance_scope": "gateway",
            "requires": [{"extension_point": "configuration", "version": 1}],
            "contributes": {"configuration": {"type": "object", "properties": {}}}}]}
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as package:
        package.writestr("plugin.jsonc", json.dumps(manifest))
    content = archive.getvalue()
    state = {"calls": [], "download_started": threading.Event(), "release_download": threading.Event(),
        "content": content, "manifest": manifest, "protocol_version": 1}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            parsed = urlsplit(self.path)
            state["calls"].append({"path": parsed.path, "query": parse_qs(parsed.query)})
            if parsed.path == "/catalog":
                body = json.dumps({"protocol_version": state["protocol_version"], "source_id": "proof",
                    "catalog_revision": "catalog-v1", "items": [state["artifact"]], "next_cursor": None}).encode()
            elif parsed.path == "/plugin.zip":
                state["download_started"].set()
                assert state["release_download"].wait(state.get("download_wait_seconds", 20)), "测试未释放下载响应"
                body = state["content"]
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/json" if parsed.path == "/catalog" else "application/zip")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    base_url = f"http://127.0.0.1:{server.server_port}"
    state["base_url"] = base_url
    state["artifact"] = {"package_id": manifest["package_id"], "package_version": manifest["version"],
        "target": None, "digest": "sha256:" + hashlib.sha256(content).hexdigest(),
        "url": base_url + "/plugin.zip", "filename": "plugin.zip"}
    (context.artifacts_dir / "source-plugin.zip").write_bytes(content)
    with running_http_server(server):
        try:
            yield state
        finally:
            state["release_download"].set()


@pytest.fixture
def source_gateway(request: pytest.FixtureRequest, integration_workspace_root_path: str, unused_tcp_port: int):
    from tests.support.processes.gateway import (
        LOCAL_TOKEN_HEADERS,
        close_gateway_process,
        start_gateway_process,
    )
    from tests.support.processes.scope import unregister_test_process
    from tests.support.processes.workspace import terminate_process

    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    # 安装控制面不调用工作区；挂接外部地址避免启动不参与本场景的 Backend。
    def start():
        return start_gateway_process(workspace_root=Path(integration_workspace_root_path),
            default_backend_url="http://127.0.0.1:1", port=unused_tcp_port,
            boxteam_home=context.boxteam_home_for_node(request.node.nodeid), artifacts_dir=context.artifacts_dir)

    handle = start()

    def restart(*, crash: bool = False):
        nonlocal handle
        assert handle is not None
        previous, handle = handle, None
        if crash:
            # 绕过 lifespan，核真实执行者死亡；Windows 仍等待自己持有的服务器子进程。
            terminate_process(previous.process, force=True, owned_children=previous.shutdown_children)
            previous.stdout_file.close()
            previous.stderr_file.close()
            unregister_test_process(previous)
        else:
            close_gateway_process(previous)
        handle = start()
    try:
        with httpx.Client(base_url=f"http://127.0.0.1:{unused_tcp_port}", headers=LOCAL_TOKEN_HEADERS,
            timeout=30, trust_env=False) as client:
            yield client, context, restart
    finally:
        if handle is not None:
            close_gateway_process(handle)


@pytest.mark.parametrize("newer_active", [False, True], ids=["discard", "newer-active"])
def test_pending_binding_blocks_uninstall_across_restart_until_explicit_discard(
    source_gateway, plugin_source: dict, request: pytest.FixtureRequest, newer_active: bool,
) -> None:
    """普通重启保留原pending；只有原配置owner显式结清后才允许卸载。"""
    client, context, restart = source_gateway
    evidence: list[dict] = []
    source_path = "/api/gateway/config/sources/gateway_mutable_override"

    def exchange(method: str, path: str, body: dict | None = None) -> httpx.Response:
        request_id = f"pending-uninstall-{len(evidence)}"
        response = client.request(method, path, json=body, headers={"X-Request-ID": request_id})
        value = response.json()
        assert response.headers["X-Request-ID"] == value["request_id"] == request_id
        evidence.append({"method": method, "path": path, "status": response.status_code, "body": value})
        return response

    try:
        gateway_id = exchange("GET", "/api/gateway/health").raise_for_status().json()["data"]["gateway_id"]
        target = {"gateway_id": gateway_id}
        archive = context.artifacts_dir / "pending-package.zip"
        archive.write_bytes(plugin_source["content"])
        install_plugin(lambda path, body: exchange("POST", path, body), gateway_id=gateway_id, path=str(archive))
        package_root = context.boxteam_home_for_node(request.node.nodeid) / "plugins/packages/source.proof/1.0.0"
        baseline = exchange("GET", source_path).raise_for_status().json()["data"]

        def save(entries: list[dict], key: str, *, restore_active: bool = False) -> dict:
            document = exchange("GET", source_path).raise_for_status().json()["data"]
            source = commentjson.loads(baseline["jsonc"] if restore_active else document["jsonc"])
            source.setdefault("plugins", {})["runtime"] = {"entries": entries}
            if restore_active:
                source.setdefault("logging", {}).setdefault("runtime", {})["level"] = "debug"
            else:
                source.setdefault("runtime", {}).setdefault("gateway", {}).setdefault("process", {}).setdefault("health", {})["request_timeout_seconds"] = 3
            return exchange("PUT", source_path, {**target, "source_key": "gateway_mutable_override",
                "jsonc": json.dumps(source), "base_file_digest": document["file_digest"],
                "base_layer_revision": document["layer_revision"], "base_layer_digest": document["layer_digest"],
                "expected_active_revision": document["active_revision"], "expected_active_digest": document["active_digest"],
                "idempotency_key": key}).raise_for_status().json()["data"]

        def uninstall() -> httpx.Response:
            return exchange("POST", "/api/gateway/service-plugins/uninstall", {
                **target, "package_id": "source.proof", "package_version": "1.0.0",
            })

        saved = save([{"package_id": "source.proof", "package_version": "1.0.0",
            "plugin_id": "declaration", "enabled": False}], "pending-binding")
        assert saved["edit"]["state"] == "pending_restart", saved
        status = exchange("POST", "/api/gateway/service-plugins/status", target).raise_for_status().json()["data"]
        assert status["configured_bindings"] == [] and status["plugins"] == [], status
        blocked = uninstall()
        assert blocked.status_code == 409 and f"candidate:{saved['edit']['candidate_id']}:declaration" in blocked.text, blocked.text
        assert package_root.is_dir()
        restart()
        status = exchange("POST", "/api/gateway/service-plugins/status", target).raise_for_status().json()["data"]
        assert status["configured_bindings"] == [] and status["plugins"] == [], status
        blocked = uninstall()
        assert blocked.status_code == 409 and f"candidate:{saved['edit']['candidate_id']}:declaration" in blocked.text, blocked.text
        if newer_active:
            # 新的合法提交不自动结清另一个原pending，不能仅检查最新candidate。
            assert save([], "newer-active", restore_active=True)["edit"]["state"] == "active"
            original = exchange("GET", source_path + "?idempotency_key=pending-binding").raise_for_status().json()["data"]
            assert original["edit"]["state"] == "pending_restart"
            blocked = uninstall()
            assert blocked.status_code == 409 and f"candidate:{saved['edit']['candidate_id']}:declaration" in blocked.text, blocked.text
            assert package_root.is_dir()
            assert plugin_source["calls"] == []
            return
        original = exchange("GET", source_path + "?idempotency_key=pending-binding").raise_for_status().json()["data"]
        assert original["edit"] == saved["edit"]
        assert original["active_revision"] == saved["active_revision"]
        discarded = exchange("POST", "/api/gateway/config/discard-restart?candidate_ref=" + original["status"]["candidate_ref"], {
            "expected_active_revision": original["active_revision"],
            "expected_active_digest": original["active_digest"],
        }).raise_for_status().json()["data"]
        assert not discarded["restart_required"] and discarded["active_revision"] == original["active_revision"], discarded
        uninstall().raise_for_status()
        assert not package_root.exists()
        assert plugin_source["calls"] == [], "本地安装、pending和重启均不应访问来源服务"
    finally:
        (context.artifacts_dir / "pending-uninstall-http.json").write_text(
            json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def test_source_install_is_queryable_cancellable_and_never_replayed(source_gateway, plugin_source: dict) -> None:
    client, context, restart = source_gateway
    evidence: list[dict] = []

    def exchange(path: str, body: dict | None = None, *, method: str = "POST", status: int = 200):
        request_id = f"install-proof-{len(evidence)}"
        response = client.request(method, path, json=body, headers={"X-Request-ID": request_id})
        value = response.json()
        evidence.append({"path": path, "status": response.status_code, "body": value})
        assert response.status_code == status, value
        assert response.headers["X-Request-ID"] == value["request_id"] == request_id
        return value.get("data")

    def settle(operation_id: str, *, timeout_seconds: float = 20):
        deadline = time.monotonic() + timeout_seconds
        while True:
            operation = exchange("/api/gateway/service-plugins/install-operation", {**target, "operation_id": operation_id})
            if operation["status"] not in {"pending", "running"}:
                return operation
            assert time.monotonic() < deadline, operation
            time.sleep(0.05)

    def source_config(enabled: bool):
        path = "/api/gateway/config/sources/gateway_mutable_override"
        document = exchange(path, method="GET")
        config = commentjson.loads(document["jsonc"])
        config.setdefault("plugins", {})["sources"] = {"entries": [{"source_id": "proof", "protocol_version": 1,
            "base_url": plugin_source["base_url"], "enabled": enabled}]}
        result = exchange(path, {**target, "source_key": "gateway_mutable_override", "jsonc": json.dumps(config),
            "base_file_digest": document["file_digest"], "base_layer_revision": document["layer_revision"],
            "base_layer_digest": document["layer_digest"], "expected_active_revision": document["active_revision"],
            "expected_active_digest": document["active_digest"], "idempotency_key": f"source-{len(evidence)}"}, method="PUT")
        assert result["edit"]["state"] == "active", result

    try:
        gateway_id = exchange("/api/gateway/health", method="GET")["gateway_id"]
        target = {"gateway_id": gateway_id}
        inventory = exchange("/api/gateway/service-plugins/inventory", target)
        assert inventory["sources"] == {"configured": False, "message": "插件源尚未配置", "entries": []}
        exchange("/api/gateway/service-plugins/source-catalog", {**target, "source_id": "proof"}, status=422)
        assert plugin_source["calls"] == []

        source_config(True)
        catalog = exchange("/api/gateway/service-plugins/source-catalog", {**target, "source_id": "proof", "query": "source"})
        assert catalog["catalog_revision"] == "catalog-v1" and len(catalog["items"]) == 1
        assert plugin_source["calls"] == [{"path": "/catalog", "query": {"query": ["source"]}}]
        plugin_source["protocol_version"] = 2
        exchange("/api/gateway/service-plugins/source-catalog", {**target, "source_id": "proof"}, status=422)
        plugin_source["protocol_version"] = 1
        acquisition = {**target, "source_id": "proof", "catalog_revision": catalog["catalog_revision"],
            "artifact": catalog["items"][0]}

        plugin_source["download_wait_seconds"] = 120
        cancelled = exchange("/api/gateway/service-plugins/install", {**acquisition, "operation_id": "cancelled-download"})
        assert cancelled["status"] in {"pending", "running"}
        assert plugin_source["download_started"].wait(5)
        exchange("/api/gateway/service-plugins/cancel-install", {**target, "operation_id": "cancelled-download"})
        assert settle("cancelled-download", timeout_seconds=3)["status"] == "cancelled"
        assert not plugin_source["release_download"].is_set(), "取消须结束等待HTTP来源，不能依靠来源恢复发送"
        plugin_source["release_download"].set()
        assert exchange("/api/gateway/service-plugins/inventory", target)["packages"] == []

        # 丢弃首个受理响应后，只用原操作查询取得最终发布事实。
        exchange("/api/gateway/service-plugins/install", {**acquisition, "operation_id": "published-download"})
        published = settle("published-download")
        assert published["status"] == "succeeded", published
        assert published["artifact"]["digest"] == acquisition["artifact"]["digest"]
        assert exchange("/api/gateway/service-plugins/status", target)["configured_bindings"] == []
        assert len(exchange("/api/gateway/service-plugins/inventory", target)["packages"]) == 1

        source_config(False)
        before = len(plugin_source["calls"])
        repeated = exchange("/api/gateway/service-plugins/install", {**acquisition, "operation_id": "published-download"})
        assert repeated == published
        assert exchange("/api/gateway/service-plugins/cancel-install", {**target,
            "operation_id": "published-download"})["status"] == "succeeded"
        assert len(plugin_source["calls"]) == before
        restart()
        assert settle("published-download") == published
        assert settle("cancelled-download")["status"] == "cancelled"
        assert len(plugin_source["calls"]) == before
        exchange("/api/gateway/service-plugins/install", {**acquisition, "operation_id": "published-download",
            "catalog_revision": "different"}, status=409)
        exchange("/api/gateway/service-plugins/install", {**acquisition, "operation_id": "disabled-source"}, status=422)
        exchange("/api/gateway/service-plugins/install-operation", {**target, "operation_id": "missing"}, status=404)

        # 下载中的关键状态由真实安装入口生成；硬退出后不能把查询变成安装恢复。
        source_config(True)
        next_manifest = {**plugin_source["manifest"], "version": "2.0.0"}
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w") as package:
            package.writestr("plugin.jsonc", json.dumps(next_manifest))
        plugin_source["content"] = archive.getvalue()
        plugin_source["artifact"] = {**plugin_source["artifact"], "package_version": "2.0.0",
            "digest": "sha256:" + hashlib.sha256(plugin_source["content"]).hexdigest()}
        catalog = exchange("/api/gateway/service-plugins/source-catalog", {**target, "source_id": "proof"})
        acquisition = {**target, "source_id": "proof", "catalog_revision": catalog["catalog_revision"],
            "artifact": catalog["items"][0]}
        plugin_source["download_started"].clear()
        plugin_source["release_download"].clear()
        accepted = exchange("/api/gateway/service-plugins/install", {**acquisition, "operation_id": "crashed-download"})
        assert accepted["status"] == "running"
        assert plugin_source["download_started"].wait(5)
        before = list(plugin_source["calls"])
        restart(crash=True)
        plugin_source["release_download"].set()
        unknown = settle("crashed-download")
        assert unknown["status"] == "unknown", unknown
        assert unknown["artifact"] == accepted["artifact"]
        assert "执行者已退出" in unknown["error"] and "不会自动重放" in unknown["error"]
        assert exchange("/api/gateway/service-plugins/install", {**acquisition, "operation_id": "crashed-download"}) == unknown
        assert exchange("/api/gateway/service-plugins/cancel-install", {**target,
            "operation_id": "crashed-download"}) == unknown
        packages = exchange("/api/gateway/service-plugins/inventory", target)["packages"]
        assert [item["manifest"]["version"] for item in packages] == ["1.0.0"]
        assert plugin_source["calls"] == before
        restart()
        assert settle("crashed-download") == unknown
        assert plugin_source["calls"] == before

        # 只有用户显式发起另一操作，才重新下载；原未知事实继续保留。
        exchange("/api/gateway/service-plugins/install", {**acquisition, "operation_id": "explicit-after-crash"})
        assert settle("explicit-after-crash")["status"] == "succeeded"
        assert len(plugin_source["calls"]) == len(before) + 1
        assert settle("crashed-download") == unknown
        assert exchange("/api/gateway/service-plugins/status", target)["configured_bindings"] == []
        for version in ("1.0.0", "2.0.0"):
            exchange("/api/gateway/service-plugins/uninstall", {**target,
                "package_id": "source.proof", "package_version": version})
        assert exchange("/api/gateway/service-plugins/inventory", target)["packages"] == []
    finally:
        (context.artifacts_dir / "installation-http.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2))
