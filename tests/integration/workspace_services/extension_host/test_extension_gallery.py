"""真实 Gateway 与固定原生安装服务：无工作区市场安装、原元数据及管理资源。"""

from __future__ import annotations

import asyncio
import json
import os
from zipfile import ZipFile

import httpx
import pytest
from fastapi import FastAPI, HTTPException, Request, Response

from app.gateway.runtime.extension_download import ExtensionDownloadRequest
from app.gateway.server import extension_management as management_routes
from tests.integration.workspace_services.extension_host.test_extension_management import (
    _restart_gateway_management,
)
from tests.integration.workspace_services.extension_host.test_extension_management import (
    management_backends as management_backends,  # noqa: PLC0414
)
from tests.integration.workspace_services.extension_host.test_extension_management import (
    management_runtime as management_runtime,  # noqa: PLC0414
)
from tests.integration.workspace_services.extension_host.test_extension_management import (
    vsix_factory as vsix_factory,  # noqa: PLC0414
)


@pytest.fixture
async def gallery_case(management_backends, vsix_factory, serve_extension_application, monkeypatch: pytest.MonkeyPatch):
    case = management_backends
    manifests = {}
    packages = {}
    downloads: list[tuple[str, str]] = []
    for name, version in [("gallery-main", "1.0.0"), ("gallery-main", "2.0.0"), ("gallery-dependency", "1.0.0")]:
        manifest = {"name": name, "publisher": "boxteam-probe", "version": version,
            "engines": {"vscode": "^1.0.0"}, "main": "./extension.js", "browser": "./extension.js",
            "extensionKind": ["workspace"], "activationEvents": ["*"],
            "extensionDependencies": ["boxteam-probe.gallery-dependency"] if name == "gallery-main" else []}
        package = vsix_factory(case.workspace, version, f"{name}@{version}", manifest_overrides=manifest)
        with ZipFile(package, "a") as archive:
            archive.writestr("extension/ReAdMe.TXT", f"# {name}@{version}\n")
            archive.writestr("extension/CHANGELOG", f"Changes {version}\n")
        manifests[name, version] = manifest
        packages[name, version] = package.read_bytes()

    gallery = FastAPI()

    @gallery.post("/gallery/extensionquery")
    async def query(request: Request):
        body = await request.json()
        requested = {criterion["value"] for group in body.get("filters", []) for criterion in group.get("criteria", [])
            if criterion["filterType"] in {4, 7}}
        extensions = []
        for name in ("gallery-main", "gallery-dependency"):
            if requested and not requested.intersection({f"boxteam-probe.{name}", f"uuid-{name}"}):
                continue
            versions = []
            for entry_name, version in manifests:
                if entry_name != name:
                    continue
                asset = f"{url}/assets/{name}/{version}"
                versions.append({"version": version, "targetPlatform": "universal", "lastUpdated": "2026-01-01T00:00:00Z",
                    "assetUri": asset, "fallbackAssetUri": asset, "files": [
                        {"assetType": kind, "source": f"{asset}/{kind}"} for kind in
                        ("Microsoft.VisualStudio.Code.Manifest", "Microsoft.VisualStudio.Services.VSIXPackage")],
                    "properties": [{"key": "Microsoft.VisualStudio.Code.Engine", "value": "^1.0.0"},
                        {"key": "Microsoft.VisualStudio.Code.PreRelease", "value": "true" if version == "2.0.0" else "false"}]})
            extensions.append({"extensionId": f"uuid-{name}", "extensionName": name, "displayName": name,
                "shortDescription": "Native gallery fixture", "publisher": {"displayName": "BoxTeam",
                    "publisherId": "publisher-id", "publisherName": "boxteam-probe"},
                "versions": versions, "statistics": [], "tags": [], "categories": [], "flags": "",
                "releaseDate": "2026-01-01T00:00:00Z", "publishedDate": "2026-01-01T00:00:00Z",
                "lastUpdated": "2026-01-01T00:00:00Z"})
        return {"results": [{"extensions": extensions, "resultMetadata": [{"metadataType": "ResultCount",
            "metadataItems": [{"name": "TotalCount", "count": len(extensions)}]}]}]}

    @gallery.get("/assets/{name}/{version}/{asset_type}")
    async def asset(name: str, version: str, asset_type: str):
        if (name, version) not in manifests:
            raise HTTPException(404, "未知测试制品")
        if asset_type == "Microsoft.VisualStudio.Code.Manifest":
            return manifests[name, version]
        if asset_type == "Microsoft.VisualStudio.Services.VSIXPackage":
            downloads.append((name, version))
            return Response(packages[name, version], media_type="application/octet-stream")
        raise HTTPException(404, "未知测试资源")

    url = await serve_extension_application(gallery)
    product = json.loads((case.context.project_root / "src/clients/workbench/upstream/product.json").read_text())
    metadata = json.loads((case.context.run_root / "runtime/version.json").read_text())
    product.update(version=metadata["version"], commit=metadata["commit"], quality="stable",
        extensionsGallery={"serviceUrl": url + "/gallery"})
    preload = case.context.artifacts_dir / "gallery-product.mjs"
    preload.write_text("globalThis._VSCODE_PRODUCT_JSON = " + json.dumps(product) + ";\n"
        + "globalThis._VSCODE_PACKAGE_JSON = " + json.dumps({"version": metadata["version"]}) + ";\n")
    monkeypatch.setenv("NODE_OPTIONS", f"{os.getenv('NODE_OPTIONS', '')} --import={preload.as_uri()}".strip())
    settings = case.domains[1].owner.directory / "data/User/settings.json"
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text('{"extensions.verifySignature": false}')
    await _restart_gateway_management(case, domain=1)
    observed: list[str] = []
    original = management_routes.extension_download

    async def capture(payload: ExtensionDownloadRequest) -> Response:
        observed.append(payload.call_site)
        return await original(payload)

    monkeypatch.setattr(management_routes, "extension_download", capture)
    return case, downloads, observed


async def test_gallery_and_metadata_use_native_target_without_workspace_or_host(gallery_case) -> None:
    case, downloads, observed = gallery_case
    a, b = case.domains
    assert not a.registry.targets() and not b.registry.targets() and not case.registries
    assert b.owner.instance is not None
    instance = b.owner.instance
    target = {"protocol_version": 1, "gateway_id": b.gateway_id, "backend_id": b.owner.backend_id}
    base = b.url + "/api/gateway/extension-installations/"
    async with httpx.AsyncClient(timeout=40, trust_env=False) as client:
        before = await client.post(a.url + "/api/gateway/extension-installations/inventory", headers=a.headers,
            json={**target, "gateway_id": a.gateway_id, "backend_id": a.owner.backend_id})
        assert before.status_code == 200, before.text

        async def install(version: str, options: dict[str, bool]):
            result = await client.post(base + "install-gallery", headers={**b.headers, "X-Request-ID": "gallery-b"},
                json={**target, "extension_id": "boxteam-probe.gallery-main", "version": version, "options": options})
            assert result.status_code == 200, result.text
            assert result.headers["X-Request-ID"] == result.json()["request_id"] == "gallery-b"
            return result.json()["data"]

        first = await install("1.0.0", {"is_machine_scoped": True, "pinned": False})
        assert first["installed"]["version"] == "1.0.0" and first["installed"]["reused"] is False
        inventory = first["inventory"]
        native_probe = await asyncio.create_subprocess_exec(str(case.runtime.executable), "-p", "process.platform + '-' + process.arch",
            stdout=asyncio.subprocess.PIPE)
        platform_output, _ = await asyncio.wait_for(native_probe.communicate(), timeout=10)
        assert native_probe.returncode == 0 and inventory["target_platform"] == platform_output.decode().strip()
        assert sorted(entry["description"]["id"] for entry in inventory["extensions"]) == [
            "boxteam-probe.gallery-dependency", "boxteam-probe.gallery-main"]
        entry = next(entry for entry in inventory["extensions"] if entry["description"]["id"] == "boxteam-probe.gallery-main")
        assert entry["metadata"]["id"] == "uuid-gallery-main" and entry["metadata"]["isMachineScoped"] is True
        assert entry["readme_path"] == "ReAdMe.TXT" and entry["changelog_path"] == "CHANGELOG"
        assert sorted(downloads) == [("gallery-dependency", "1.0.0"), ("gallery-main", "1.0.0")]
        assert observed and set(observed) == {"extensionGalleryService.download"}
        asset_root = (f"{b.url}/api/gateway/extension-packages/current/{b.gateway_id}/{b.owner.backend_id}/"
            f"{inventory['profile_id']}/{entry['extension_key']}/boxteam-probe.gallery-main/")
        readme = await client.get(asset_root + entry["readme_path"], headers=b.headers)
        assert readme.status_code == 200 and readme.text == "# gallery-main@1.0.0\n"
        assert readme.headers["Cache-Control"] == "no-store"
        member = {**target, "profile_id": inventory["profile_id"], "extension_id": "boxteam-probe.gallery-main", "version": "1.0.0"}
        changed = await client.post(base + "metadata", headers=b.headers, json={**member,
            "metadata": {"pinned": True, "is_machine_scoped": False, "pre_release": True}, "refresh_from_gallery": True})
        assert changed.status_code == 200, changed.text
        updated = next(entry for entry in changed.json()["data"]["inventory"]["extensions"]
            if entry["description"]["id"] == "boxteam-probe.gallery-main")
        assert updated["metadata"]["pinned"] is True and updated["metadata"]["preRelease"] is True
        assert not updated["metadata"].get("isMachineScoped", False)
        assert updated["metadata"]["id"] == "uuid-gallery-main"

        second = await install("2.0.0", {"install_pre_release_version": True, "install_given_version": True})
        assert second["installed"]["version"] == "2.0.0" and second["installed"]["reused"] is False
        latest = next(entry for entry in second["inventory"]["extensions"] if entry["description"]["id"] == "boxteam-probe.gallery-main")
        assert latest["metadata"]["isPreReleaseVersion"] is True and latest["metadata"]["pinned"] is True
        assert (await client.get(asset_root + "ReAdMe.TXT", headers=b.headers)).status_code == 409
        count = len(downloads)
        reused = await install("2.0.0", {"install_pre_release_version": True, "install_given_version": True})
        assert reused["installed"]["reused"] is True and len(downloads) == count
        stale = await client.post(base + "metadata", headers=b.headers, json={**member, "metadata": {"pinned": False}})
        assert stale.status_code == 409
        invalid = await b.owner.request({"method": "metadata", "profile_id": inventory["profile_id"],
            "extension_id": "boxteam-probe.gallery-main", "version": "2.0.0",
            "metadata": {"id": "forged-uuid"}, "refresh_from_gallery": False})
        assert "error" in invalid and "result" not in invalid
        after = await client.post(a.url + "/api/gateway/extension-installations/inventory", headers=a.headers,
            json={**target, "gateway_id": a.gateway_id, "backend_id": a.owner.backend_id})
        assert after.json()["data"] == before.json()["data"]
        assert b.owner.instance == instance and b.owner.process is not None and b.owner.process.is_alive()
    assert not case.registries and not b.registry.targets()
