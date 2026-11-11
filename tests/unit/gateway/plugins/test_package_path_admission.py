from __future__ import annotations

import json
import stat
import zipfile
from pathlib import Path

import pytest

from app.gateway.plugins.packages import PluginPackageStore


def _manifest() -> dict[str, object]:
    return {
        "manifest_version": 1,
        "package_id": "admission.probe",
        "version": "1.0.0",
        "name": "Admission probe",
        "plugins": [
            {
                "id": "probe",
                "name": "Probe",
                "instance_scope": "gateway",
                "requires": [{"extension_point": "services", "version": 1}],
                "contributes": {
                    "services": [
                        {"service_id": "probe", "service_type": "browser", "protocol": "http"}
                    ]
                },
            }
        ],
    }


def _write_zip(path: Path, entries: dict[str, bytes]) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("plugin.jsonc", json.dumps(_manifest()).encode())
        for name, content in entries.items():
            archive.writestr(name, content)


@pytest.mark.parametrize(
    ("member", "escape_path"),
    [
        ("absolute", "absolute-target.txt"),
        ("parent", "package-parent-target.txt"),
    ],
)
def test_install_zip_rejects_absolute_and_parent_entries_before_writing(
    tmp_path: Path, member: str, escape_path: str,
) -> None:
    installed_root = tmp_path / "installed"
    target = (
        installed_root / "admission.probe" / escape_path
        if member == "parent"
        else tmp_path / escape_path
    )
    archive_path = tmp_path / "candidate.zip"
    archive_member = target.as_posix() if member == "absolute" else "../package-parent-target.txt"
    _write_zip(archive_path, {archive_member: b"must not be written"})
    store = PluginPackageStore(installed_root)

    with pytest.raises(ValueError):
        store.install_zip(archive_path)

    package_root = installed_root / "admission.probe"
    assert not target.exists()
    assert not (package_root / "1.0.0").exists()
    assert not list(package_root.glob(".install-*"))


def test_install_zip_rejects_external_symlink_before_writing(tmp_path: Path) -> None:
    installed_root = tmp_path / "installed"
    target = tmp_path / "external-target.txt"
    archive_path = tmp_path / "candidate.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("plugin.jsonc", json.dumps(_manifest()).encode())
        link = zipfile.ZipInfo("links/external.txt")
        link.create_system = 3
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        archive.writestr(link, "../../external-target.txt")
    store = PluginPackageStore(installed_root)

    with pytest.raises(ValueError):
        store.install_zip(archive_path)

    assert not target.exists()
    assert not (installed_root / "admission.probe/1.0.0").exists()
    assert not list((installed_root / "admission.probe").glob(".install-*"))


def test_install_directory_rejects_external_symlink_before_writing(tmp_path: Path) -> None:
    installed_root = tmp_path / "installed"
    source = tmp_path / "development-package"
    source.mkdir()
    (source / "plugin.jsonc").write_text(json.dumps(_manifest()), encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    marker = outside / "marker.txt"
    marker.write_text("outside package", encoding="utf-8")
    try:
        (source / "outside-link").symlink_to(outside, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"当前系统不允许创建目录符号链接: {error}")
    store = PluginPackageStore(installed_root)

    with pytest.raises(ValueError):
        store.install_directory(source)

    assert marker.read_text(encoding="utf-8") == "outside package"
    assert not (installed_root / "admission.probe/1.0.0").exists()
    assert not list((installed_root / "admission.probe").glob(".install-*"))
