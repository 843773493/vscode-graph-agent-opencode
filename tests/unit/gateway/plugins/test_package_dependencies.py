from __future__ import annotations

import json
import shutil
import zipfile
from pathlib import Path

import pytest

from app.gateway.plugins import dependencies
from app.gateway.plugins.packages import PluginPackageStore
from tests.support.plugin_package import native_plugin_target


def _manifest(runtime: str, package_id: str = "prebuilt.probe") -> dict[str, object]:
    return {
        "manifest_version": 1,
        "package_id": package_id,
        "version": "1.0.0",
        "name": "预构建依赖探针",
        "extension_contract_version": 1,
        "target": native_plugin_target(runtime),
        "plugins": [{
            "id": "probe", "name": "Probe", "instance_scope": "gateway",
            "requires": [{"extension_point": "services", "version": 1}],
            "contributes": {"services": [{"service_id": "probe", "service_type": "terminal", "protocol": "http"}]},
            "runtime": runtime, "entrypoint": ["entry.py" if runtime == "python" else "entry.mjs"],
            "management_protocol_version": 1,
        }],
    }


def _python_archive(path: Path) -> None:
    project = """[project]
name = "python-prebuilt-fixture"
version = "1.0.0"
requires-python = ">=3.12"
dependencies = ["first-dependency==1.0.0"]
"""
    lock = """version = 1
revision = 3
requires-python = ">=3.12"

[[package]]
name = "first-dependency"
version = "1.0.0"
source = { registry = "https://pypi.org/simple" }
dependencies = [{ name = "transitive-dependency" }]

[[package]]
name = "transitive-dependency"
version = "2.0.0"
source = { registry = "https://pypi.org/simple" }

[[package]]
name = "python-prebuilt-fixture"
version = "1.0.0"
source = { virtual = "." }
dependencies = [{ name = "first-dependency" }]

[package.metadata]
requires-dist = [{ name = "first-dependency", specifier = "==1.0.0" }]
"""
    files = {
        "plugin.jsonc": json.dumps(_manifest("python")),
        "pyproject.toml": project,
        "uv.lock": lock,
        "entry.py": "from first_dependency import VALUE\nprint(VALUE)\n",
        "python/site-packages/first_dependency/__init__.py": "VALUE = 'prebuilt-python-ready'\n",
        "python/site-packages/first_dependency-1.0.0.dist-info/METADATA":
            "Metadata-Version: 2.1\nName: first-dependency\nVersion: 1.0.0\nRequires-Dist: transitive-dependency==2.0.0\n\n",
        "python/site-packages/transitive_dependency/__init__.py": "VALUE = 2\n",
        "python/site-packages/transitive_dependency-2.0.0.dist-info/METADATA":
            "Metadata-Version: 2.1\nName: transitive-dependency\nVersion: 2.0.0\n\n",
    }
    with zipfile.ZipFile(path, "w") as archive:
        for name, content in files.items():
            archive.writestr(name, content)


def _node_archive(path: Path) -> None:
    manifest = _manifest("node", "prebuilt.node")
    package = {"name": "node-prebuilt-fixture", "version": "1.0.0", "dependencies": {"node-dependency": "1.0.0"}}
    lock = {
        "lockfileVersion": 1,
        "workspaces": {"": {"name": "node-prebuilt-fixture", "dependencies": {"node-dependency": "1.0.0"}}},
        "packages": {
            "node-dependency": ["node-dependency@1.0.0", "", {"node-transitive": "1.0.0"}, "sha512-fixture"],
            "node-transitive": ["node-transitive@1.0.0", "", {}, "sha512-fixture"],
        },
    }
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("plugin.jsonc", json.dumps(manifest))
        archive.writestr("package.json", json.dumps(package))
        archive.writestr("bun.lock", json.dumps(lock))
        archive.writestr("entry.mjs", "import { value } from 'node-dependency'; console.log(value);\n")
        archive.writestr("node_modules/node-dependency/package.json",
            json.dumps({"name": "node-dependency", "version": "1.0.0", "type": "module", "exports": "./index.js",
                        "dependencies": {"node-transitive": "1.0.0"}}))
        archive.writestr("node_modules/node-dependency/index.js", "export const value = 'prebuilt-node-ready';\n")
        archive.writestr("node_modules/node-transitive/package.json",
            json.dumps({"name": "node-transitive", "version": "1.0.0", "type": "module"}))
        archive.writestr("node_modules/node-transitive/index.js", "export const transitive = true;\n")


def test_python_package_installs_offline_with_complete_transitive_dependencies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = native_plugin_target("python")
    monkeypatch.setattr(dependencies, "_python_runtime", lambda _path: target["python"])
    archive = tmp_path / "python.zip"
    _python_archive(archive)

    installed = PluginPackageStore(tmp_path / "packages").install_zip(archive)
    site = PluginPackageStore(tmp_path / "packages").python_site_packages(
        "prebuilt.probe", "1.0.0", plugin_id="probe",
    )

    assert site == installed.root / "python/site-packages"
    assert (site / "first_dependency/__init__.py").is_file()
    assert (site / "transitive_dependency/__init__.py").is_file()
    assert not (installed.root / ".venv").exists()


def test_node_package_installs_offline_with_prebuilt_node_modules(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("测试主机没有 Node")
    target = native_plugin_target("node")
    monkeypatch.setenv("BOXTEAM_NODE_BIN", str(Path(node).resolve(strict=True)))
    monkeypatch.setattr(dependencies, "_node_runtime", lambda _path: target["node"])
    archive = tmp_path / "node.zip"
    _node_archive(archive)

    installed = PluginPackageStore(tmp_path / "packages").install_zip(archive)

    assert (installed.root / "node_modules/node-dependency/package.json").is_file()
    assert (installed.root / "node_modules/node-dependency/index.js").is_file()
    assert (installed.root / "node_modules/node-transitive/package.json").is_file()


def test_missing_node_transitive_dependency_rejects_before_publish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("测试主机没有 Node")
    target = native_plugin_target("node")
    monkeypatch.setenv("BOXTEAM_NODE_BIN", str(Path(node).resolve(strict=True)))
    monkeypatch.setattr(dependencies, "_node_runtime", lambda _path: target["node"])
    complete = tmp_path / "complete.zip"
    _node_archive(complete)
    incomplete = tmp_path / "incomplete.zip"
    with zipfile.ZipFile(complete) as source, zipfile.ZipFile(incomplete, "w") as output:
        for entry in source.infolist():
            if entry.filename.startswith("node_modules/node-transitive/"):
                continue
            output.writestr(entry.filename, source.read(entry.filename))

    store = PluginPackageStore(tmp_path / "packages")
    with pytest.raises(ValueError, match="node-transitive"):
        store.install_zip(incomplete)
    assert not (tmp_path / "packages/prebuilt.node/1.0.0").exists()


def test_node_package_name_must_match_the_locked_dependency_identity_before_publish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("测试主机没有 Node")
    target = native_plugin_target("node")
    monkeypatch.setenv("BOXTEAM_NODE_BIN", str(Path(node).resolve(strict=True)))
    monkeypatch.setattr(dependencies, "_node_runtime", lambda _path: target["node"])
    complete = tmp_path / "complete.zip"
    _node_archive(complete)
    mismatched = tmp_path / "mismatched-package-name.zip"
    with zipfile.ZipFile(complete) as source, zipfile.ZipFile(mismatched, "w") as output:
        for entry in source.infolist():
            content = source.read(entry.filename)
            if entry.filename == "node_modules/node-dependency/package.json":
                package = json.loads(content)
                package["name"] = "different-package"
                content = json.dumps(package).encode()
            output.writestr(entry.filename, content)

    store = PluginPackageStore(tmp_path / "packages")
    with pytest.raises(ValueError, match="版本与 bun.lock 不一致"):
        store.install_zip(mismatched)
    assert not (tmp_path / "packages/prebuilt.node/1.0.0").exists()


def test_node_alias_uses_locked_package_identity_and_remains_installable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("测试主机没有 Node")
    target = native_plugin_target("node")
    monkeypatch.setenv("BOXTEAM_NODE_BIN", str(Path(node).resolve(strict=True)))
    monkeypatch.setattr(dependencies, "_node_runtime", lambda _path: target["node"])
    complete = tmp_path / "complete.zip"
    _node_archive(complete)
    alias_archive = tmp_path / "alias.zip"
    with zipfile.ZipFile(complete) as source, zipfile.ZipFile(alias_archive, "w") as output:
        for entry in source.infolist():
            name, content = entry.filename, source.read(entry.filename)
            if name == "package.json":
                package = json.loads(content)
                package["dependencies"] = {"dependency-alias": "npm:node-dependency@1.0.0"}
                content = json.dumps(package).encode()
            elif name == "bun.lock":
                lock = json.loads(content)
                lock["workspaces"][""]["dependencies"] = {
                    "dependency-alias": "npm:node-dependency@1.0.0",
                }
                lock["packages"]["dependency-alias"] = lock["packages"].pop("node-dependency")
                content = json.dumps(lock).encode()
            elif name.startswith("node_modules/node-dependency/"):
                name = name.replace("node_modules/node-dependency/", "node_modules/dependency-alias/", 1)
            output.writestr(name, content)

    installed = PluginPackageStore(tmp_path / "packages").install_zip(alias_archive)
    assert (installed.root / "node_modules/dependency-alias/package.json").is_file()


def test_missing_python_transitive_dependency_rejects_before_publish(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = native_plugin_target("python")
    monkeypatch.setattr(dependencies, "_python_runtime", lambda _path: target["python"])
    archive = tmp_path / "missing-transitive.zip"
    _python_archive(archive)
    with zipfile.ZipFile(archive) as source, zipfile.ZipFile(tmp_path / "incomplete.zip", "w") as output:
        for entry in source.infolist():
            if entry.filename.startswith("python/site-packages/transitive_dependency"):
                continue
            output.writestr(entry.filename, source.read(entry.filename))

    store = PluginPackageStore(tmp_path / "packages")
    with pytest.raises(ValueError, match="transitive-dependency"):
        store.install_zip(tmp_path / "incomplete.zip")
    assert not (tmp_path / "packages/prebuilt.probe/1.0.0").exists()


def test_lock_mismatch_rejects_before_publish(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = native_plugin_target("node")
    monkeypatch.setenv("BOXTEAM_NODE_BIN", str(Path(shutil.which("node") or "node").resolve(strict=True)))
    monkeypatch.setattr(dependencies, "_node_runtime", lambda _path: target["node"])
    archive = tmp_path / "conflict.zip"
    _node_archive(archive)
    with zipfile.ZipFile(archive) as source, zipfile.ZipFile(tmp_path / "changed.zip", "w") as output:
        for entry in source.infolist():
            content = source.read(entry.filename)
            if entry.filename == "bun.lock":
                lock = json.loads(content)
                lock["workspaces"][""]["dependencies"]["node-dependency"] = "2.0.0"
                content = json.dumps(lock).encode()
            output.writestr(entry.filename, content)

    with pytest.raises(ValueError, match="与 package.json 不一致"):
        PluginPackageStore(tmp_path / "packages").install_zip(tmp_path / "changed.zip")
