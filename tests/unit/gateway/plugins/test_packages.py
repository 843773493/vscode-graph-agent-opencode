from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from app.gateway.plugins.packages import PluginPackageStore


def _manifest() -> dict[str, object]:
    return {
        "manifest_version": 1,
        "package_id": "example.tools",
        "version": "1.0.0",
        "name": "Example tools",
        "extension_contract_version": 1,
        "plugins": [
            {
                "id": "browser",
                "name": "Browser",
                "instance_scope": "workspace",
                "requires": [
                    {"extension_point": "services", "version": 1},
                    {"extension_point": "skills", "version": 1},
                ],
                "contributes": {
                    "services": [
                        {"service_id": "browser", "service_type": "browser", "protocol": "http"}
                    ],
                    "skills": [{"id": "browser-control", "path": "custom/docs/browser-guide"}],
                },
                "runtime": "node",
                "entrypoint": ["implementations/engine/browser/start.mjs", "--managed"],
                "management_protocol_version": 1,
            }
        ],
    }


def _write_zip(path: Path, files: dict[str, bytes]) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        for name, content in files.items():
            archive.writestr(name, content)


def _install_manifest(tmp_path: Path, manifest: dict[str, object]) -> None:
    archive_path = tmp_path / "candidate.zip"
    _write_zip(archive_path, {
        "plugin.jsonc": json.dumps(manifest).encode(),
        "implementations/engine/browser/start.mjs": b"export {};\n",
        "custom/docs/browser-guide/SKILL.md": (
            b"---\nname: browser-control\ndescription: Browser guidance\n---\n\nUse browser.\n"
        ),
    })
    PluginPackageStore(tmp_path / "installed").install_zip(archive_path)


def test_nested_declared_entrypoint_and_skill_are_installed_without_directory_discovery(tmp_path: Path) -> None:
    archive_path = tmp_path / "package.zip"
    _write_zip(archive_path, {
        "plugin.jsonc": json.dumps(_manifest()).encode(),
        "implementations/engine/browser/start.mjs": b"export {}\n",
        "custom/docs/browser-guide/SKILL.md": b"---\nname: browser-control\ndescription: Browser guidance\n---\n\nUse browser.\n",
        "custom/docs/browser-guide/examples/action.md": b"example\n",
        "unlisted/stray/plugin.jsonc": b"not another plugin manifest\n",
    })

    installed = PluginPackageStore(tmp_path / "installed").install_zip(archive_path)

    assert installed.manifest.plugins[0].entrypoint == ["implementations/engine/browser/start.mjs", "--managed"]
    assert (installed.root / "implementations/engine/browser/start.mjs").is_file()
    assert (installed.root / "custom/docs/browser-guide/examples/action.md").is_file()
    assert len(installed.manifest.plugins) == 1


@pytest.mark.parametrize(
    ("missing_path", "message"),
    [
        ("implementations/engine/browser/start.mjs", "入口文件不存在"),
        ("custom/docs/browser-guide/SKILL.md", "缺少 SKILL.md"),
    ],
)
def test_nested_declared_files_must_exist(tmp_path: Path, missing_path: str, message: str) -> None:
    archive_path = tmp_path / "package.zip"
    files = {
        "plugin.jsonc": json.dumps(_manifest()).encode(),
        "implementations/engine/browser/start.mjs": b"export {}\n",
        "custom/docs/browser-guide/SKILL.md": b"---\nname: browser-control\ndescription: Browser guidance\n---\n",
    }
    files.pop(missing_path)
    _write_zip(archive_path, files)

    with pytest.raises(ValueError, match=message):
        PluginPackageStore(tmp_path / "installed").install_zip(archive_path)


def test_nested_entrypoint_cannot_escape_package_root(tmp_path: Path) -> None:
    manifest = _manifest()
    plugin = manifest["plugins"][0]  # type: ignore[index]
    plugin["entrypoint"] = ["implementations/../../outside.mjs"]  # type: ignore[index]
    archive_path = tmp_path / "package.zip"
    _write_zip(archive_path, {"plugin.jsonc": json.dumps(manifest).encode()})

    with pytest.raises(ValueError, match="relative POSIX path|越界|相对 POSIX 路径"):
        PluginPackageStore(tmp_path / "installed").install_zip(archive_path)


def test_explicit_development_directory_publishes_a_manifest_driven_snapshot(tmp_path: Path) -> None:
    source = tmp_path / "development-package"
    (source / "implementations/engine/browser").mkdir(parents=True)
    (source / "custom/docs/browser-guide/examples").mkdir(parents=True)
    (source / "plugin.jsonc").write_text(json.dumps(_manifest()), encoding="utf-8")
    entrypoint = source / "implementations/engine/browser/start.mjs"
    entrypoint.write_text("export const version = 1;\n", encoding="utf-8")
    skill = source / "custom/docs/browser-guide/SKILL.md"
    skill.write_text(
        "---\nname: browser-control\ndescription: Browser guidance\n---\n\nUse browser.\n",
        encoding="utf-8",
    )
    (source / "custom/docs/browser-guide/examples/action.md").write_text("example\n", encoding="utf-8")
    (source / "unlisted/stray").mkdir(parents=True)
    (source / "unlisted/stray/plugin.jsonc").write_text("not another plugin manifest\n", encoding="utf-8")

    installed = PluginPackageStore(tmp_path / "installed").install_directory(source)
    entrypoint.write_text("export const version = 2;\n", encoding="utf-8")

    assert installed.manifest.plugins[0].entrypoint == [
        "implementations/engine/browser/start.mjs", "--managed",
    ]
    assert (installed.root / "implementations/engine/browser/start.mjs").read_text(encoding="utf-8") == (
        "export const version = 1;\n"
    )
    assert (installed.root / "custom/docs/browser-guide/examples/action.md").is_file()
    assert len(installed.manifest.plugins) == 1


def test_install_accepts_typed_plugin_contributions(tmp_path: Path) -> None:
    manifest = _manifest()
    plugin = manifest["plugins"][0]  # type: ignore[index]
    plugin["requires"].extend(  # type: ignore[index]
        [
            {"extension_point": "commands", "version": 1},
            {"extension_point": "menus", "version": 1},
            {"extension_point": "keybindings", "version": 1},
            {"extension_point": "configuration", "version": 1},
            {"extension_point": "debuggers", "version": 1},
        ]
    )
    plugin["contributes"].update(  # type: ignore[index]
        {
            "commands": [{
                "id": "browser.open", "title": "Open Browser", "handler": "openBrowser",
                "arguments_schema": {"type": "object"}, "result_schema": {"type": "object"},
            }],
            "menus": [{"id": "browser.open.menu", "location": "editor/context", "command_id": "browser.open"}],
            "keybindings": [{"id": "browser.open.key", "command_id": "browser.open", "key": "ctrl+alt+b"}],
            "configuration": {"type": "object", "properties": {"enabled": {"type": "boolean"}}},
            "debuggers": [{
                "type": "example.debug", "label": "Example", "configuration_schema": {"type": "object"},
                "service_id": "browser", "dap_version": 1,
            }],
        }
    )
    plugin["contributes"]["skills"][0]["tool_bindings"] = [{  # type: ignore[index]
        "tool_id": "browser.inspect", "contract_version": 1, "service_id": "browser",
    }]

    _install_manifest(tmp_path, manifest)


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda manifest: manifest["plugins"][0]["requires"].append(
            {"extension_point": "not_supported", "version": 1}), "未知或未开放"),
        (lambda manifest: manifest["plugins"][0]["requires"][0].update(version=2), "版本不兼容"),
        (lambda manifest: manifest["plugins"][0]["contributes"]["skills"].append(
            {"id": "browser-control", "path": "custom/docs/browser-guide"}), "不得重复"),
        (lambda manifest: manifest["plugins"][0]["contributes"].update({
            "skills": [{"id": "browser-control", "path": "custom/docs/browser-guide", "tool_bindings": [
                {"tool_id": "browser.inspect", "contract_version": 1, "service_id": "foreign-service"},
            ]}],
        }), "未导出的服务"),
        (lambda manifest: manifest["plugins"][0]["contributes"].update({
            "skills": [{"id": "browser-control", "path": "custom/docs/browser-guide", "tool_bindings": [
                {"tool_id": "arbitrary.tool", "contract_version": 1},
            ]}],
        }), "未列入产品批准集合"),
        (lambda manifest: manifest["plugins"][0]["contributes"].update({
            "debuggers": [{
                "type": "example.debug", "label": "Example", "configuration_schema": {"type": "object"},
                "service_id": "foreign-service", "dap_version": 1,
            }],
        }) or manifest["plugins"][0]["requires"].append({"extension_point": "debuggers", "version": 1}),
         "必须引用本插件导出的服务"),
        (lambda manifest: manifest["plugins"][0]["contributes"].update({
            "commands": [{"id": "browser.open", "title": "Open", "handler": "open", "arguments_schema": {}, "result_schema": {}}],
            "menus": [{"id": "browser.menu", "location": "editor/context", "command_id": "foreign.command"}],
        }) or manifest["plugins"][0]["requires"].extend([
            {"extension_point": "commands", "version": 1},
            {"extension_point": "menus", "version": 1},
        ]), "只能引用本插件命令"),
        (lambda manifest: manifest["plugins"][0]["contributes"].update({
            "commands": [{"id": "browser.open", "title": "Open", "handler": "bad handler", "arguments_schema": {}, "result_schema": {}}],
        }) or manifest["plugins"][0]["requires"].append({"extension_point": "commands", "version": 1}), "handler"),
        (lambda manifest: manifest["plugins"][0]["contributes"].update({
            "commands": [{"id": "browser.open", "title": "Open", "handler": "open", "arguments_schema": {"type": "not-a-schema-type"}, "result_schema": {}}],
        }) or manifest["plugins"][0]["requires"].append({"extension_point": "commands", "version": 1}), "不是有效 JSON Schema"),
        (lambda manifest: manifest["plugins"][0]["contributes"].update({
            "commands": [{"id": "browser.open", "title": "Open", "handler": "open", "arguments_schema": {}, "result_schema": {},
                          "required_capabilities": ["unknown_permission"]}],
        }) or manifest["plugins"][0]["requires"].append({"extension_point": "commands", "version": 1}), "未知业务权限"),
    ],
)
def test_install_rejects_invalid_contribution_contracts(
    tmp_path: Path, mutate: object, message: str,
) -> None:
    manifest = _manifest()
    mutate(manifest)  # type: ignore[operator]

    with pytest.raises(ValueError, match=message):
        _install_manifest(tmp_path, manifest)
