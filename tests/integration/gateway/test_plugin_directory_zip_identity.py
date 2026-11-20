"""真实 Gateway 安装入口核对目录与 ZIP 的制品身份边界。"""

from __future__ import annotations

import base64
import hashlib
import json
import shutil
import tarfile
import zipfile
from contextlib import ExitStack
from io import BytesIO
from pathlib import Path

import commentjson
import httpx
import pytest

from tests.harness.python.run_context import TestRunContext
from tests.support.plugin_package import expect_plugin_install_failure, install_plugin
from tests.support.processes.gateway import (
    LOCAL_TOKEN_HEADERS,
    close_gateway_process,
    start_gateway_process,
)


def _write_zip(source: Path, archive: Path) -> None:
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as package:
        for file in sorted(source.rglob("*")):
            if file.is_file():
                package.write(file, file.relative_to(source).as_posix())


def _change_sdk_identity(source: Path, *, name: str | None = None, version: str | None = None) -> None:
    manifest_path = source / "package.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    dependency = manifest["dependencies"]["@boxteam/plugin-sdk"]
    relative = dependency.removeprefix("file:")
    sdk_archive = source / relative
    with tarfile.open(sdk_archive, "r:gz") as bundle:
        members = [(member, bundle.extractfile(member).read() if member.isfile() else None)
                   for member in bundle.getmembers()]
    output = BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as bundle:
        for member, contents in members:
            if member.name == "package/package.json":
                sdk_manifest = json.loads(contents)
                if name is not None:
                    sdk_manifest["name"] = name
                if version is not None:
                    sdk_manifest["version"] = version
                contents = (json.dumps(sdk_manifest, separators=(",", ":")) + "\n").encode()
                member.size = len(contents)
            bundle.addfile(member, BytesIO(contents) if contents is not None else None)
    sdk_archive.write_bytes(output.getvalue())
    digest = hashlib.sha256(sdk_archive.read_bytes()).hexdigest()
    old_parts = Path(relative).parts
    new_relative = "/".join((*old_parts[:2], digest, old_parts[-1]))
    new_archive = source / new_relative
    new_archive.parent.mkdir(parents=True)
    sdk_archive.rename(new_archive)
    manifest["dependencies"]["@boxteam/plugin-sdk"] = f"file:{new_relative}"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    lock_path = source / "bun.lock"
    lock = commentjson.loads(lock_path.read_text(encoding="utf-8"))
    old_reference = lock["packages"]["@boxteam/plugin-sdk"]
    lock["workspaces"][""]["dependencies"]["@boxteam/plugin-sdk"] = f"file:{new_relative}"
    lock["packages"]["@boxteam/plugin-sdk"] = [
        f"@boxteam/plugin-sdk@{new_relative}", old_reference[1],
        "sha512-" + base64.b64encode(hashlib.sha512(new_archive.read_bytes()).digest()).decode(),
    ]
    lock_path.write_text(json.dumps(lock), encoding="utf-8")


def test_directory_and_zip_install_preserve_source_identity_in_two_homes(
    request: pytest.FixtureRequest,
    integration_workspace_root_path: str,
    unused_tcp_port_factory,
    prepared_developer_tools_package: Path,
) -> None:
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    artifacts = context.artifacts_dir
    source = artifacts / "developer-tools"
    shutil.copytree(
        prepared_developer_tools_package,
        source,
        ignore=shutil.ignore_patterns(".boxteam-plugin.json"),
        copy_function=shutil.copyfile,
    )
    archive = artifacts / "developer-tools.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as package:
        for file in sorted(source.rglob("*")):
            if file.is_file():
                package.write(file, file.relative_to(source).as_posix())

    def inventory(client: httpx.Client, gateway_id: str) -> list[dict[str, object]]:
        return client.post("/api/gateway/service-plugins/inventory", json={
            "gateway_id": gateway_id,
        }).raise_for_status().json()["data"]["packages"]

    with ExitStack() as cleanup:
        for index, first_source in enumerate(("directory", "zip")):
            workspace = Path(integration_workspace_root_path) / f"home-{index}"
            workspace.mkdir(parents=True, exist_ok=True)
            home = context.boxteam_home_for_node(f"{request.node.nodeid}-home-{index}")
            gateway = start_gateway_process(
                workspace_root=workspace,
                default_backend_url="http://127.0.0.1:1",
                port=unused_tcp_port_factory(),
                boxteam_home=home,
                artifacts_dir=artifacts / f"gateway-{index}",
            )
            cleanup.callback(close_gateway_process, gateway)
            with httpx.Client(
                base_url=f"http://127.0.0.1:{gateway.port}",
                headers=LOCAL_TOKEN_HEADERS,
                timeout=60,
                trust_env=False,
            ) as client:
                gateway_id = client.get("/api/gateway/health").raise_for_status().json()["data"]["gateway_id"]

                def post(path: str, body: dict[str, object]) -> httpx.Response:
                    return client.post(path, json=body)

                first_path = str(source if first_source == "directory" else archive)
                first_is_directory = first_source == "directory"
                first = install_plugin(post, gateway_id=gateway_id, path=first_path,
                    development_directory=first_is_directory)
                repeated = install_plugin(post, gateway_id=gateway_id, path=first_path,
                    development_directory=first_is_directory)
                assert repeated.artifact == first.artifact
                installed = inventory(client, gateway_id)
                assert len(installed) == 1 and installed[0]["digest"] == first.artifact.digest.removeprefix("sha256:")

                if index == 0:
                    manifest_path, lock_path = source / "package.json", source / "bun.lock"
                    original_manifest, original_lock = manifest_path.read_bytes(), lock_path.read_bytes()
                    sdk_relative = json.loads(original_manifest)["dependencies"]["@boxteam/plugin-sdk"].removeprefix("file:")
                    sdk_archive = source / sdk_relative
                    original_sdk_archive = sdk_archive.read_bytes()

                    def restore_sdk_inputs(archive_path: Path, archive_bytes: bytes,
                                           package_manifest_path: Path, package_manifest: bytes,
                                           package_lock_path: Path, package_lock: bytes) -> None:
                        shutil.rmtree(source / "vendor" / "sdk")
                        archive_path.parent.mkdir(parents=True)
                        archive_path.write_bytes(archive_bytes)
                        package_manifest_path.write_bytes(package_manifest)
                        package_lock_path.write_bytes(package_lock)

                    for case in ("tampered", "missing", "escape", "wrong-name", "wrong-version"):
                        restore_sdk_inputs(sdk_archive, original_sdk_archive, manifest_path, original_manifest,
                                           lock_path, original_lock)
                        if case == "tampered":
                            sdk_archive.write_bytes(original_sdk_archive + b"tamper")
                        elif case == "missing":
                            sdk_archive.unlink()
                        elif case == "escape":
                            manifest = json.loads(original_manifest)
                            escaped = "vendor/sdk/../../outside.tgz"
                            manifest["dependencies"]["@boxteam/plugin-sdk"] = f"file:{escaped}"
                            manifest_path.write_text(json.dumps(manifest))
                            lock = commentjson.loads(original_lock)
                            lock["workspaces"][""]["dependencies"]["@boxteam/plugin-sdk"] = f"file:{escaped}"
                            lock["packages"]["@boxteam/plugin-sdk"][0] = f"@boxteam/plugin-sdk@{escaped}"
                            lock_path.write_text(json.dumps(lock))
                        elif case == "wrong-name":
                            _change_sdk_identity(source, name="@boxteam/other-sdk")
                        else:
                            _change_sdk_identity(source, version="0.0.999")

                        rejection = expect_plugin_install_failure(post, gateway_id=gateway_id,
                            path=str(source), development_directory=True)
                        assert rejection.error, f"损坏场景 {case} 必须返回具体错误"
                        assert inventory(client, gateway_id) == installed, f"损坏场景 {case} 改变了安装库存"
                        assert client.post("/api/gateway/service-plugins/status", json={
                            "gateway_id": gateway_id,
                        }).raise_for_status().json()["data"]["configured_bindings"] == [], f"损坏场景 {case} 改变了绑定"
                    restore_sdk_inputs(sdk_archive, original_sdk_archive, manifest_path, original_manifest,
                                       lock_path, original_lock)

                other_path = str(archive if first_is_directory else source)
                rejected = expect_plugin_install_failure(post, gateway_id=gateway_id,
                    path=other_path, development_directory=not first_is_directory)
                assert rejected.error is not None
                assert "不可变" in rejected.error, "目录与 ZIP 的同版本来源冲突必须由不可变版本规则拒绝"
                after = client.post("/api/gateway/service-plugins/inventory", json={
                    "gateway_id": gateway_id,
                }).raise_for_status().json()["data"]["packages"]
                assert after == installed
