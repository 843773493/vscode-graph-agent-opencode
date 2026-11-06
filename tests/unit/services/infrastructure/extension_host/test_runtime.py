"""专用运行时必须来自明确 manifest，不借应用通用 Node 或当前目录。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from app.services.infrastructure.extension_host.runtime import load_node_host_runtime


@pytest.fixture
def runtime_manifest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    component = tmp_path / "component"
    component.mkdir()
    version = {"commit": "a" * 40, "version": "1.117.0", "nodeVersion": "24.22.1"}
    for name, value in {"version.json": version, "package.json": {}, "package-lock.json": {}}.items():
        (component / name).write_text(json.dumps(value), encoding="utf-8")
    (component / "extensionHostProcess.mjs").write_text("// 装具不执行进程。\n", encoding="utf-8")
    manifest = tmp_path / "runtime-manifest.json"
    manifest.write_text(json.dumps({
        "schema_version": 1, "distribution": "source-development", "version": "0.0.1",
        "python_executable": sys.executable, "application_root": str(tmp_path),
        "config_resources": {key: f"{key}.jsonc" for key in (
            "gateway_inline", "gateway_schema", "workspace_inline", "workspace_schema")},
        "skill_resources": "skills", "node": {"source": "launcher", "executable": None},
        "extension_host": {"node_executable": sys.executable, "component_root": "component"},
    }), encoding="utf-8")
    monkeypatch.setenv("BOXTEAM_RUNTIME_MANIFEST", str(manifest))
    return manifest


def test_explicit_binding_resolves_from_manifest_and_matches_public_schema(
    runtime_manifest: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    schema = json.loads(Path("packages/launcher/runtime-manifest.schema.json").read_text())
    Draft202012Validator(schema).validate(json.loads(runtime_manifest.read_text()))
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    runtime = load_node_host_runtime()
    assert runtime is not None
    assert runtime.executable == Path(sys.executable).resolve()
    assert runtime.entrypoint == runtime_manifest.parent / "component/extensionHostProcess.mjs"
    assert (runtime.commit, runtime.vscode_version, runtime.node_version) == ("a" * 40, "1.117.0", "24.22.1")


@pytest.mark.parametrize("mode", ["no_manifest", "absent", "null"])
def test_undeclared_host_is_unavailable_without_launcher_node_fallback(
    runtime_manifest: Path, monkeypatch: pytest.MonkeyPatch, mode: str,
) -> None:
    monkeypatch.setenv("BOXTEAM_NODE_BIN", sys.executable)
    if mode == "no_manifest":
        monkeypatch.delenv("BOXTEAM_RUNTIME_MANIFEST")
    else:
        payload = json.loads(runtime_manifest.read_text())
        payload.pop("extension_host")
        if mode == "null":
            payload["extension_host"] = None
        runtime_manifest.write_text(json.dumps(payload))
    assert load_node_host_runtime() is None


@pytest.mark.parametrize("binding", [
    {}, {"node_executable": "node"},
    {"node_executable": "", "component_root": "component"},
    {"node_executable": "  ", "component_root": "component"},
    {"node_executable": "node", "component_root": ""},
    {"node_executable": "node", "component_root": "\t"},
    {"node_executable": 1, "component_root": "component"},
    {"node_executable": "node", "component_root": "component", "command": "override"},
])
def test_malformed_binding_is_rejected_by_python_and_manifest_schema(
    runtime_manifest: Path, binding: object,
) -> None:
    payload = json.loads(runtime_manifest.read_text())
    payload["extension_host"] = binding
    schema = json.loads(Path("packages/launcher/runtime-manifest.schema.json").read_text())
    assert list(Draft202012Validator(schema).iter_errors(payload))
    runtime_manifest.write_text(json.dumps(payload))
    with pytest.raises(ValidationError):
        load_node_host_runtime()


@pytest.mark.parametrize("missing", ["node", "extensionHostProcess.mjs", "version.json", "package.json", "package-lock.json"])
def test_configured_missing_component_is_an_error(runtime_manifest: Path, missing: str) -> None:
    if missing == "node":
        payload = json.loads(runtime_manifest.read_text())
        payload["extension_host"]["node_executable"] = "missing-node"
        runtime_manifest.write_text(json.dumps(payload))
    else:
        (runtime_manifest.parent / "component" / missing).unlink()
    with pytest.raises(FileNotFoundError):
        load_node_host_runtime()


@pytest.mark.parametrize("contents", ["[]", "{", '{"schema_version": 2}'])
def test_invalid_manifest_does_not_look_unavailable(runtime_manifest: Path, contents: str) -> None:
    runtime_manifest.write_text(contents)
    with pytest.raises(ValueError):
        load_node_host_runtime()


def test_malformed_component_version_is_not_accepted(runtime_manifest: Path) -> None:
    (runtime_manifest.parent / "component/version.json").write_text(
        '{"commit":"unverified", "version":"1.0.0", "nodeVersion":"24"}')
    with pytest.raises(ValidationError):
        load_node_host_runtime()
