"""原扩展搜索与详情操作沿独立目标；Gallery 和 SSH 传输为具名装具。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import subprocess
import sys
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit
from uuid import NAMESPACE_URL, uuid5
from zipfile import ZipFile

import commentjson
import pytest

from app.core.backend_identity import load_or_create_backend_id
from app.gateway.federation.ports import principal_ref_for_gateway
from tests.harness.python.web_workbench import (
    web_workbench_assets as web_workbench_assets,  # noqa: PLC0414
)
from tests.integration.gateway.test_target_gateway_routing import (
    TargetGatewayNetwork,
)
from tests.integration.gateway.test_target_gateway_routing import (
    target_gateway_network as target_gateway_network,  # noqa: PLC0414
)
from tests.integration.workspace_services.extension_host.test_extension_management import (
    management_runtime as management_runtime,  # noqa: PLC0414
)
from tests.integration.workspace_services.extension_host.test_extension_management import (
    vsix_factory as vsix_factory,  # noqa: PLC0414
)
from tests.support.http_server import running_http_server
from tests.support.paths import chromium_temporary_environment


@pytest.fixture(autouse=True)
def extension_management_environment(
    management_runtime, runtime_manifest_path: Path, web_workbench_assets: Path,
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch, vsix_factory,
) -> Iterator[dict[str, object]]:
    context, component, runtime = management_runtime
    directory = context.runtime_root_for_node(request.node.nodeid)
    directory.mkdir(parents=True, exist_ok=True)
    for label in ("a", "b"):
        home = directory / f"gateway-{label}-home"
        backend_id = load_or_create_backend_id(home)
        settings = home / "state/backend/extension-management" / backend_id / "data/User/settings.json"
        settings.parent.mkdir(parents=True, exist_ok=True)
        # 受控Gallery无签名；在原Node启动前设置本次隔离profile，不伪造签名或安装事实。
        settings.write_text('{"extensions.verifySignature":false}', encoding="utf-8")
    version = "1.0.0"
    manifests: dict[str, dict[str, object]] = {}
    packages: dict[str, bytes] = {}
    observed: list[dict[str, object]] = []
    for name in ("view-alpha", "view-beta"):
        manifest = {"name": name, "publisher": "boxteam-probe", "version": version,
            "displayName": "Target " + name.removeprefix("view-").title(), "engines": {"vscode": "^1.145.0"},
            "browser": "./extension.js", "extensionKind": ["web"], "contributes": {}}
        package = vsix_factory(directory, version, name, manifest_overrides=manifest, omit=("main", "activationEvents"))
        with ZipFile(package, "a") as archive:
            archive.writestr("extension/README.md", f"# {manifest['displayName']} {version}\n")
        manifests[name] = manifest
        packages[name] = package.read_bytes()

    class GalleryHandler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *arguments: object) -> None:
            observed.append({"http": format % arguments})

        def reply(self, value: object, status: int = 200, media_type: str = "application/json") -> None:
            content = value if isinstance(value, bytes) else json.dumps(value).encode()
            self.send_response(status)
            self.send_header("Content-Type", media_type)
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Headers", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.end_headers()
            self.wfile.write(content)

        def do_OPTIONS(self) -> None:
            self.reply(b"")

        def gallery_extension(self, name: str) -> dict[str, object]:
            asset = f"{url}/assets/{name}/{version}"
            return {"extensionId": str(uuid5(NAMESPACE_URL, name)), "extensionName": name, "displayName": manifests[name]["displayName"],
                "shortDescription": "独立目标详情验收", "publisher": {"publisherName": "boxteam-probe", "publisherId": "target-publisher", "displayName": "BoxTeam"},
                "versions": [{"version": version, "targetPlatform": "universal", "lastUpdated": "2026-11-08T00:00:00Z",
                    "assetUri": asset, "fallbackAssetUri": asset,
                    "files": [{"assetType": kind, "source": f"{asset}/{kind}"} for kind in (
                        "Microsoft.VisualStudio.Code.Manifest", "Microsoft.VisualStudio.Services.VSIXPackage", "Microsoft.VisualStudio.Services.Content.Details")],
                    "properties": [{"key": "Microsoft.VisualStudio.Code.Engine", "value": "^1.145.0"},
                        {"key": "Microsoft.VisualStudio.Code.ExtensionKind", "value": "web"},
                        {"key": "Microsoft.VisualStudio.Code.WebExtension", "value": "true"},
                        {"key": "PrivateMarketplace", "value": "true"}]}],
                "statistics": [], "tags": ["__web_extension"], "categories": [], "flags": "",
                "releaseDate": "2026-11-08T00:00:00Z", "publishedDate": "2026-11-08T00:00:00Z", "lastUpdated": "2026-11-08T00:00:00Z"}

        def do_POST(self) -> None:
            payload = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))) or b"{}")
            if urlsplit(self.path).path != "/gallery/extensionquery":
                self.reply({"error": "未知 Gallery 命令"}, 404)
                return
            observed.append({"query": payload})
            requested = {criterion["value"] for group in payload.get("filters", []) for criterion in group.get("criteria", [])
                if criterion["filterType"] in {4, 7}}
            extensions = [self.gallery_extension(name) for name in manifests
                if not requested or requested.intersection({"boxteam-probe." + name, str(uuid5(NAMESPACE_URL, name))})]
            self.reply({"results": [{"extensions": extensions, "resultMetadata": [{"metadataType": "ResultCount",
                "metadataItems": [{"name": "TotalCount", "count": len(extensions)}]}]}]})

        def do_GET(self) -> None:
            parts = urlsplit(self.path).path.strip("/").split("/")
            if len(parts) == 5 and parts[:3] == ["gallery", "vscode", "boxteam-probe"] and parts[4] == "latest" and parts[3] in manifests:
                self.reply(self.gallery_extension(parts[3]))
                return
            if len(parts) != 4 or parts[0] != "assets":
                self.reply({"error": "未知 Gallery 资源"}, 404)
                return
            _, name, requested_version, kind = parts
            if name not in manifests or requested_version != version:
                self.reply({"error": "未知 Gallery 版本"}, 404)
            elif kind == "Microsoft.VisualStudio.Code.Manifest":
                self.reply(manifests[name])
            elif kind == "Microsoft.VisualStudio.Services.VSIXPackage":
                observed.append({"download": name, "version": version})
                self.reply(packages[name], media_type="application/octet-stream")
            elif kind == "Microsoft.VisualStudio.Services.Content.Details":
                self.reply(f"# {name} {version}\n".encode(), media_type="text/plain")
            else:
                self.reply({"error": "未知 Gallery 资源类型"}, 404)

    with running_http_server(ThreadingHTTPServer(("127.0.0.1", 0), GalleryHandler)) as server:
        url = f"http://127.0.0.1:{server.server_port}"
        metadata = json.loads((component / "version.json").read_text(encoding="utf-8"))
        product = json.loads((Path.cwd() / "src/clients/workbench/upstream/product.json").read_text(encoding="utf-8"))
        product.update(version=metadata["version"], commit=metadata["commit"], quality="stable", extensionsGallery={"serviceUrl": url + "/gallery"})
        preload = directory / "gallery-product.mjs"
        preload.write_text("globalThis._VSCODE_PRODUCT_JSON = " + json.dumps(product) + ";\n"
            + "globalThis._VSCODE_PACKAGE_JSON = " + json.dumps({"version": metadata["version"]}) + ";\n", encoding="utf-8")
        monkeypatch.setenv("NODE_OPTIONS", f"{os.getenv('NODE_OPTIONS', '')} --import={preload.as_uri()}".strip())
        manifest = json.loads(runtime_manifest_path.read_text(encoding="utf-8"))
        manifest["extension_host"] = {"node_executable": str(runtime.executable), "component_root": str(component)}
        manifest_path = directory / "extension-runtime.json"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        monkeypatch.setenv("BOXTEAM_RUNTIME_MANIFEST", str(manifest_path))
        monkeypatch.setenv("BOXTEAM_WEB_ASSETS", str(web_workbench_assets))
        try:
            yield {"product": product, "gallery_url": url, "version": metadata["version"]}
        finally:
            (context.artifacts_dir / "gallery-requests.json").write_text(json.dumps(observed, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


async def test_original_extension_details_keep_target_after_switch(
    target_gateway_network: TargetGatewayNetwork, extension_management_environment: dict[str, object],
) -> None:
    network = target_gateway_network
    local, remote = network.clients
    gateway_a, gateway_b = network.gateway_ids
    context = network.context
    evidence = []

    def exchange(client, method: str, path: str, body: dict[str, object] | None = None):
        response = client.request(method, path, json=body)
        result = response.json()
        evidence.append({"method": method, "path": path, "status": response.status_code, "body": result})
        assert response.is_success, response.text
        assert result["request_id"] == response.headers["X-Request-ID"]
        return result["data"]

    source = exchange(remote, "GET", "/api/gateway/config/sources/gateway_mutable_override")
    config = commentjson.loads(source["jsonc"])
    config.setdefault("permissions", {})["federation"] = {"default_effect": "allow", "rules": [
        {"capability": capability, "effect": "allow", "principal_ref": principal_ref_for_gateway(gateway_a)}
        for capability in ("extension_installation_manage", "extension_host_manage")]}
    exchange(remote, "PUT", "/api/gateway/config/sources/gateway_mutable_override", {
        "gateway_id": gateway_b, "source_key": "gateway_mutable_override", "jsonc": json.dumps(config),
        "base_file_digest": source["file_digest"], "base_layer_revision": source["layer_revision"],
        "base_layer_digest": source["layer_digest"], "expected_active_revision": source["active_revision"],
        "expected_active_digest": source["active_digest"], "idempotency_key": "extension-management-target-policy"})
    topology = json.loads((context.artifacts_dir / "topology.json").read_text(encoding="utf-8"))
    exchange(local, "POST", "/api/gateway/remote-gateways", {"name": "Extension Target B", "host": "127.0.0.1", "port": 22,
        "username": "extension-target", "private_key_path": topology["private_key"], "remote_gateway_port": topology["ports"][3]})
    targets = {}
    for label, client, gateway_id in zip(("a", "b"), network.clients, network.gateway_ids, strict=True):
        directory = exchange(client, "POST", "/api/gateway/extension-targets", {"protocol_version": 1, "gateway_id": gateway_id})
        target, = directory["targets"]
        targets[label] = {"gateway_id": gateway_id, "backend_id": target["backend_id"]}
    (context.artifacts_dir / "preparation.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    chromium = os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH") or shutil.which("chromium")
    assert chromium, "原扩展视图验收需要真实Chromium"
    output = context.artifacts_dir / "extension-management-browser.json"
    supervisor = Path.cwd() / "app/runtime" / ("windows_job.py" if sys.platform == "win32" else "posix_job.py")
    command = [sys.executable, str(supervisor), "--memory-limit-kb", "4194304", "--timeout-seconds", "150", "--",
        "bun", "tests/integration/clients/web/extension_management_target.mjs"]
    runtime = context.runtime_root_for_node(os.environ["PYTEST_CURRENT_TEST"])
    with chromium_temporary_environment(runtime / "chromium-tmp") as temporary:
        result = await asyncio.to_thread(subprocess.run, command, cwd=Path.cwd(), capture_output=True, text=True, timeout=165, check=False,
            env={**os.environ, **temporary, "PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH": chromium,
                "BOXTEAM_BROWSER_RESULT_PATH": str(output), "BOXTEAM_EXTENSION_MANAGEMENT_TARGET": json.dumps({
                    **extension_management_environment, "base_url": str(local.base_url).rstrip("/"), "targets": targets})})
    (context.artifacts_dir / "browser.stdout.raw").write_text(result.stdout, encoding="utf-8")
    (context.artifacts_dir / "browser.stderr.raw").write_text(result.stderr, encoding="utf-8")
    paths = ["src/clients/workbench/scripts/extensions/extensionManagementTransform.mjs", "src/clients/workbench/src/extensions/nativeManagement.ts",
        "src/clients/workbench/src/managementTarget.ts", "tests/integration/clients/web/extension_management_target.mjs",
        "tests/integration/clients/web/test_extension_management_target.py"]
    (context.artifacts_dir / "browser-process.json").write_text(json.dumps({"command": command, "exit_code": result.returncode,
        "sha256": {path: hashlib.sha256((Path.cwd() / path).read_bytes()).hexdigest() for path in paths}}, indent=2) + "\n", encoding="utf-8")
    assert result.returncode == 0, result.stdout + result.stderr
    browser = json.loads(output.read_text(encoding="utf-8"))
    assert browser["phase"] == "complete" and browser["pageErrors"] == []
    assert browser["writes"] and all(item["backend_id"] == targets["b"]["backend_id"] for item in browser["writes"][:2])
