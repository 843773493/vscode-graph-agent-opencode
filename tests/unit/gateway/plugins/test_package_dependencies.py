from __future__ import annotations

import base64
import hashlib
import http.server
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import zipfile
from io import BytesIO
from pathlib import Path

import pytest

from app.gateway.plugins.dependencies import _node_headers_root, _npm_runtime
from app.gateway.plugins.packages import PluginPackageStore


def _wheel() -> bytes:
    output = BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("plugin_dependency/__init__.py", "VALUE = 'private-python-dependency'\n")
        archive.writestr("plugin_dependency-1.0.0.dist-info/METADATA",
            "Metadata-Version: 2.1\nName: plugin-dependency\nVersion: 1.0.0\n\n")
        archive.writestr("plugin_dependency-1.0.0.dist-info/WHEEL",
            "Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\nTag: py3-none-any\n")
        archive.writestr("plugin_dependency-1.0.0.dist-info/RECORD", "")
    return output.getvalue()


def _npm_archive() -> bytes:
    output = BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        for name, content in (
            ("package/package.json", b'{"name":"plugin-dependency","version":"1.0.0","type":"module","exports":"./index.js"}'),
            ("package/index.js", b"export const value = 'private-node-dependency';\n"),
        ):
            info = tarfile.TarInfo(name)
            info.size = len(content)
            archive.addfile(info, BytesIO(content))
    return output.getvalue()


class _RegistryHandler(http.server.BaseHTTPRequestHandler):
    wheel: bytes
    npm_archive: bytes
    port: int
    requests: list[str]
    fail_wheel: bool

    def do_HEAD(self) -> None:
        self.requests.append(self.path)
        if self.path == "/plugin_dependency-1.0.0-py3-none-any.whl":
            body = self.wheel
        elif self.path == "/plugin-dependency-1.0.0.tgz":
            body = self.npm_archive
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()

    def do_GET(self) -> None:
        self.requests.append(self.path)
        if self.path == "/plugin_dependency-1.0.0-py3-none-any.whl" and self.fail_wheel:
            self.send_error(404)
            return
        if self.path == "/simple/plugin-dependency/":
            body = (f'<a href="http://127.0.0.1:{self.port}/plugin_dependency-1.0.0-py3-none-any.whl">'
                "plugin_dependency-1.0.0-py3-none-any.whl</a>").encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
        elif self.path == "/plugin_dependency-1.0.0-py3-none-any.whl":
            body = self.wheel
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
        elif self.path == "/plugin-dependency":
            integrity = "sha512-" + base64.b64encode(hashlib.sha512(self.npm_archive).digest()).decode()
            body = json.dumps({
                "name": "plugin-dependency",
                "dist-tags": {"latest": "1.0.0"},
                "versions": {"1.0.0": {
                    "name": "plugin-dependency", "version": "1.0.0", "type": "module",
                    "exports": "./index.js",
                    "dist": {"tarball": f"http://127.0.0.1:{self.port}/plugin-dependency-1.0.0.tgz", "integrity": integrity},
                }},
            }).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
        elif self.path == "/plugin-dependency-1.0.0.tgz":
            body = self.npm_archive
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
        else:
            self.send_error(404)
            return
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format: str, *_args: object) -> None:
        return


@pytest.fixture
def local_registry() -> str:
    handler = type("LocalRegistryHandler", (_RegistryHandler,), {
        "wheel": _wheel(), "npm_archive": _npm_archive(), "port": 0,
        "requests": [], "fail_wheel": False,
    })
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    handler.port = server.server_port
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", handler.requests, handler
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def _plugin_manifest(runtime: str, entrypoint: str) -> dict[str, object]:
    return {
        "manifest_version": 1,
        "package_id": f"locked.{runtime}",
        "version": "1.0.0",
        "name": f"Locked {runtime}",
        "extension_contract_version": 1,
        "plugins": [{
            "id": "probe",
            "name": "Dependency probe",
            "instance_scope": "gateway",
            "requires": [{"extension_point": "services", "version": 1}],
            "contributes": {"services": [{"service_id": "probe", "service_type": "terminal", "protocol": "http"}]},
            "runtime": runtime,
            "entrypoint": [entrypoint],
            "management_protocol_version": 1,
        }],
    }


def _make_python_archive(workspace: Path, registry: str) -> Path:
    project = workspace / "python"
    project.mkdir()
    (project / "pyproject.toml").write_text(
        "[project]\nname = 'plugin-dependency-fixture'\nversion = '1.0.0'\n"
        f"requires-python = '=={sys.version_info.major}.{sys.version_info.minor}.*'\n"
        "dependencies = ['plugin-dependency==1.0.0']\n",
        encoding="utf-8",
    )
    environment = {key: value for key, value in os.environ.items()
        if not key.upper().startswith(("UV_", "PIP_", "PYTHON"))
        and key not in {"VIRTUAL_ENV", "CONDA_PREFIX"}}
    subprocess.run([
        shutil.which("uv") or "uv", "--no-config", "--project", str(project), "lock",
        "--python", sys.executable, "--no-python-downloads", "--no-managed-python",
        "--default-index", registry + "/simple",
    ], cwd=project, env=environment, capture_output=True, text=True, timeout=45, check=True)
    (project / "plugin.jsonc").write_text(json.dumps(_plugin_manifest("python", "entry.py")), encoding="utf-8")
    (project / "entry.py").write_text("from plugin_dependency import VALUE\nprint(VALUE)\n", encoding="utf-8")
    archive = workspace / "python-plugin.zip"
    with zipfile.ZipFile(archive, "w") as output:
        for item in sorted(project.iterdir()):
            output.write(item, item.name)
    return archive


def _make_node_archive(workspace: Path, registry: str, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    configured_node = shutil.which("node")
    assert configured_node is not None
    monkeypatch.setenv("BOXTEAM_NODE_BIN", configured_node)
    node, npm = _npm_runtime()
    project = workspace / "node"
    project.mkdir()
    (project / "package.json").write_text(json.dumps({
        "name": "locked-node-fixture", "version": "1.0.0", "private": True,
        "type": "module", "dependencies": {"plugin-dependency": "1.0.0"},
    }), encoding="utf-8")
    with tempfile.TemporaryDirectory(dir=workspace) as temporary:
        config = Path(temporary)
        (config / "user.npmrc").touch()
        (config / "global.npmrc").touch()
        subprocess.run([
            str(node), str(npm), "--prefix", str(project), "--registry", registry,
            "--userconfig", str(config / "user.npmrc"), "--globalconfig", str(config / "global.npmrc"),
            "--cache", str(config / "cache"), "--ignore-scripts", "--no-audit", "--no-fund",
            "install", "--package-lock-only",
        ], cwd=project, capture_output=True, text=True, timeout=45, check=True)
    (project / "plugin.jsonc").write_text(json.dumps(_plugin_manifest("node", "entry.mjs")), encoding="utf-8")
    (project / "entry.mjs").write_text(
        "import { value } from 'plugin-dependency'; console.log(value);\n", encoding="utf-8",
    )
    archive = workspace / "node-plugin.zip"
    with zipfile.ZipFile(archive, "w") as output:
        for item in sorted(project.iterdir()):
            output.write(item, item.name)
    return archive, node


def test_locked_python_and_node_dependencies_are_prepared_before_atomic_publish(
    tmp_path: Path, local_registry: tuple[str, list[str], type[_RegistryHandler]], monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry, requests, registry_state = local_registry
    python_archive = _make_python_archive(tmp_path, registry)
    node_archive, node = _make_node_archive(tmp_path, registry, monkeypatch)
    store = PluginPackageStore(tmp_path / "installed")

    failed_archive = tmp_path / "failed-plugin.zip"
    with zipfile.ZipFile(python_archive) as source, zipfile.ZipFile(failed_archive, "w") as output:
        for entry in source.infolist():
            content = source.read(entry.filename)
            if entry.filename == "plugin.jsonc":
                manifest = json.loads(content)
                manifest["version"] = "1.0.1"
                content = json.dumps(manifest).encode()
            output.writestr(entry.filename, content)
    registry_state.fail_wheel = True
    with pytest.raises(RuntimeError, match="stage=python-sync"):
        store.install_zip(failed_archive)
    failed_package_directory = tmp_path / "installed/locked.python/1.0.1"
    assert not failed_package_directory.exists()
    assert not list(failed_package_directory.parent.glob(".install-*"))
    registry_state.fail_wheel = False

    python_package = store.install_zip(python_archive)
    python = store.python_executable("locked.python", "1.0.0", plugin_id="probe")
    assert python is not None and python.is_file()
    python_result = subprocess.run(
        [str(python), str(python_package.root / "entry.py")],
        capture_output=True, text=True, timeout=15, check=True,
    )
    assert python_result.stdout.strip() == "private-python-dependency"

    node_package = store.install_zip(node_archive)
    node_result = subprocess.run(
        [str(node), str(node_package.root / "entry.mjs")],
        capture_output=True, text=True, timeout=15, check=True,
    )
    assert node_result.stdout.strip() == "private-node-dependency"
    assert (node_package.root / "node_modules/plugin-dependency/package.json").is_file()

    before = python_package.root / ".venv"
    assert before.is_dir()
    requests_before_reuse = len(requests)
    assert store.install_zip(python_archive).root == python_package.root
    assert len(requests) == requests_before_reuse, "同摘要包安装必须复用，不重新访问依赖源"


def test_dependency_lock_mismatch_rejects_package_before_publish(tmp_path: Path) -> None:
    manifest = _plugin_manifest("node", "entry.mjs")
    package_json = {"name": "locked-node-fixture", "version": "1.0.0", "dependencies": {"one": "1.0.0"}}
    package_lock = {
        "name": "locked-node-fixture", "version": "1.0.0", "lockfileVersion": 3,
        "packages": {"": {"name": "locked-node-fixture", "version": "1.0.0", "dependencies": {"one": "2.0.0"}}},
    }
    archive = tmp_path / "conflict.zip"
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr("plugin.jsonc", json.dumps(manifest))
        output.writestr("package.json", json.dumps(package_json))
        output.writestr("package-lock.json", json.dumps(package_lock))
        output.writestr("entry.mjs", "export {}\n")

    store = PluginPackageStore(tmp_path / "installed")
    with pytest.raises(ValueError, match="与 package.json 不一致"):
        store.install_zip(archive)
    assert not (tmp_path / "installed/locked.node/1.0.0").exists()


@pytest.mark.parametrize("binary_parts", [("bin", "node"), ("node",)])
def test_node_headers_root_supports_binary_and_bin_layouts(tmp_path: Path, binary_parts: tuple[str, ...]) -> None:
    install_root = tmp_path / "node"
    executable = install_root.joinpath(*binary_parts)
    executable.parent.mkdir(parents=True)
    executable.touch()
    (install_root / "include/node").mkdir(parents=True)
    (install_root / "include/node/node.h").touch()

    assert _node_headers_root(executable) == install_root
