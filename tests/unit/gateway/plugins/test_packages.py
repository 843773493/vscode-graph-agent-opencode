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
