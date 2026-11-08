"""Gateway 安装域中的不可变自有插件包。"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
import tomllib
import zipfile
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal

import commentjson
import portalocker

from app.core.atomic_fs import fsync_directory, fsync_file
from app.schemas.gateway.service_plugin import PluginPackageManifest

_MAX_ARCHIVE_BYTES = 256 * 1024 * 1024
_MAX_EXPANDED_BYTES = 512 * 1024 * 1024
_MAX_FILES = 20_000
_VERSION_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.+_-]{0,127}$")


@dataclass(frozen=True, slots=True)
class InstalledPluginPackage:
    manifest: PluginPackageManifest
    digest: str
    root: Path

    def model_dump(self, *, mode: Literal["python", "json"] = "python") -> dict[str, object]:
        """公开库存省略机器本地安装目录。"""
        if mode not in {"python", "json"}:
            raise ValueError(f"不支持插件包序列化模式: {mode}")
        return self.inventory_item()

    def inventory_item(self) -> dict[str, object]:
        return {"manifest": self.manifest.model_dump(mode="json"), "digest": self.digest}


class PluginPackageStore:
    """唯一安装域包目录；安装串行发布，运行期只读已发布版本。"""

    def __init__(self, root: Path) -> None:
        root.mkdir(parents=True, exist_ok=True)
        self.root = root.resolve(strict=True)
        self._lock_path = self.root / ".install.lock"

    def install_zip(self, path: Path, expected_digest: str | None = None) -> InstalledPluginPackage:
        archive_path = path.resolve(strict=True)
        if not archive_path.is_file() or archive_path.stat().st_size > _MAX_ARCHIVE_BYTES:
            raise ValueError("插件包必须是大小不超过 256 MiB 的 ZIP 文件")
        raw_digest = _sha256_file(archive_path)
        if expected_digest is not None and expected_digest.lower().removeprefix("sha256:") != raw_digest:
            raise ValueError("插件包摘要与请求不匹配")
        with zipfile.ZipFile(archive_path) as archive:
            entries = _zip_entries(archive)
            manifest_info = entries.get("plugin.jsonc")
            if manifest_info is None or manifest_info.is_dir():
                raise ValueError("自有插件 ZIP 根目录必须包含 plugin.jsonc")
            if sum(item.file_size for item in entries.values()) > _MAX_EXPANDED_BYTES:
                raise ValueError("插件 ZIP 解压大小超过 512 MiB")
            manifest = _parse_manifest(archive.read(manifest_info))
            files = {name for name, info in entries.items() if not info.is_dir()}
            read_file = lambda relative: archive.read(entries[relative])
            _validate_package_files(manifest, files, read_file)
            with portalocker.Lock(self._lock_path, mode="a+b", timeout=30):
                def prepare(destination: Path) -> None:
                    _extract(archive, entries, destination)
                    if _sha256_file(archive_path) != raw_digest:
                        raise RuntimeError("ZIP 源文件在发布期间发生变化；未发布包")
                return self._publish(manifest, raw_digest, prepare)

    def install_directory(self, path: Path) -> InstalledPluginPackage:
        if path.is_symlink():
            raise ValueError("开发插件源不能是符号链接")
        source = path.resolve(strict=True)
        if not source.is_dir():
            raise ValueError("开发插件源必须是普通目录")
        entries = _directory_entries(source)
        if sum((source / name).stat().st_size for name in entries) > _MAX_EXPANDED_BYTES:
            raise ValueError("插件目录大小超过 512 MiB")
        manifest_path = source / "plugin.jsonc"
        if "plugin.jsonc" not in entries:
            raise ValueError("插件目录根目录必须包含 plugin.jsonc")
        manifest = _parse_manifest(manifest_path.read_bytes())
        _validate_package_files(manifest, entries, lambda relative: (source / relative).read_bytes())
        digest = _directory_digest(source, entries)
        with portalocker.Lock(self._lock_path, mode="a+b", timeout=30):
            def prepare(destination: Path) -> None:
                _copy_directory(source, entries, destination)
                if _directory_digest(destination, entries) != digest:
                    raise RuntimeError("开发插件源在发布期间发生变化；未发布包")
            return self._publish(manifest, digest, prepare)

    def inventory(self) -> list[InstalledPluginPackage]:
        installed: list[InstalledPluginPackage] = []
        with portalocker.Lock(self._lock_path, mode="a+b", timeout=30):
            for package_dir in sorted(self.root.iterdir()):
                if package_dir.name.startswith("."):
                    continue
                if package_dir.is_symlink() or not package_dir.is_dir():
                    raise RuntimeError(f"插件库存包含非目录安装项: {package_dir.name}")
                for version_dir in sorted(package_dir.iterdir()):
                    if version_dir.name.startswith("."):
                        continue
                    installed.append(_read_installed(version_dir))
        return installed

    def get(self, package_id: str, version: str) -> InstalledPluginPackage:
        _validate_identity(package_id, version)
        return _read_installed(self.root / package_id / version)

    def package_root(self, package_id: str, version: str) -> Path:
        return self.get(package_id, version).root

    def uninstall(self, package_id: str, version: str, *, references: Iterable[str] = ()) -> None:
        _validate_identity(package_id, version)
        package_dir = self.root / package_id
        target = package_dir / version
        with portalocker.Lock(self._lock_path, mode="a+b", timeout=30):
            installed = _read_installed(target)
            active_references = tuple(references)
            if active_references:
                raise RuntimeError("插件包仍被引用，不能卸载: " + ", ".join(active_references))
            shutil.rmtree(installed.root)
            if package_dir.exists() and not any(package_dir.iterdir()):
                package_dir.rmdir()

    def _publish(self, manifest: PluginPackageManifest, digest: str,
                 prepare: Callable[[Path], None]) -> InstalledPluginPackage:
        package_dir = self.root / manifest.package_id
        destination = package_dir / manifest.version
        _validate_identity(manifest.package_id, manifest.version)
        if package_dir.is_symlink():
            raise RuntimeError("插件安装域包目录不能是符号链接")
        package_dir.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            installed = _read_installed(destination)
            if installed.digest != digest:
                raise RuntimeError(f"插件包版本不可变且摘要冲突: {manifest.package_id}@{manifest.version}")
            return installed
        staging = Path(tempfile.mkdtemp(prefix=".install-", dir=package_dir))
        try:
            prepare(staging)
            staged_entries = _directory_entries(staging)
            staged_manifest = _parse_manifest((staging / "plugin.jsonc").read_bytes())
            if staged_manifest != manifest:
                raise RuntimeError("发布副本中的 plugin.jsonc 与已校验版本不同")
            _validate_package_files(
                staged_manifest, staged_entries,
                lambda relative: (staging / relative).read_bytes(),
            )
            metadata = {"digest": digest, "manifest": manifest.model_dump(mode="json")}
            metadata_path = staging / ".boxteam-plugin.json"
            with metadata_path.open("x", encoding="utf-8") as stream:
                stream.write(json.dumps(metadata, ensure_ascii=False, separators=(",", ":")))
                stream.flush()
                os.fsync(stream.fileno())
            _sync_tree(staging)
            os.replace(staging, destination)
            fsync_directory(package_dir)
        finally:
            if staging.exists():
                shutil.rmtree(staging)
        return InstalledPluginPackage(manifest=manifest, digest=digest, root=destination)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_member(name: str) -> str:
    if not name or "\x00" in name or "\\" in name or name.startswith("/"):
        raise ValueError(f"插件包路径必须是相对 POSIX 路径: {name!r}")
    raw_name = name.removesuffix("/")
    raw_parts = raw_name.split("/")
    path = PurePosixPath(raw_name)
    if not path.parts or any(part in {"", ".", ".."} or ":" in part for part in raw_parts):
        raise ValueError(f"插件包路径越界: {name!r}")
    if path.as_posix() != "/".join(raw_parts):
        raise ValueError(f"插件包路径含非规范分隔: {name!r}")
    return path.as_posix()


def _zip_entries(archive: zipfile.ZipFile) -> dict[str, zipfile.ZipInfo]:
    infos = archive.infolist()
    if len(infos) > _MAX_FILES:
        raise ValueError("插件 ZIP 文件数量超过限制")
    entries: dict[str, zipfile.ZipInfo] = {}
    folded_names: set[str] = set()
    for info in infos:
        name = _safe_member(info.filename)
        if name.casefold() in folded_names:
            raise ValueError(f"插件 ZIP 含重复路径: {name}")
        mode = info.external_attr >> 16
        if stat.S_ISLNK(mode):
            raise ValueError(f"插件 ZIP 不允许符号链接: {name}")
        file_type = stat.S_IFMT(mode)
        if file_type not in {0, stat.S_IFREG, stat.S_IFDIR}:
            raise ValueError(f"插件 ZIP 含不支持的文件类型: {name}")
        if info.flag_bits & 0x1:
            raise ValueError(f"插件 ZIP 不支持加密条目: {name}")
        folded_names.add(name.casefold())
        entries[name] = info
    if ".boxteam-plugin.json" in entries:
        raise ValueError("包内不能包含安装器保留的元数据文件")
    return entries


def _directory_entries(source: Path) -> tuple[str, ...]:
    entries: list[str] = []
    for item in source.rglob("*"):
        relative = item.relative_to(source).as_posix()
        _safe_member(relative)
        if item.is_symlink():
            raise ValueError(f"插件目录不允许符号链接: {relative}")
        if item.is_file():
            entries.append(relative)
        elif not item.is_dir():
            raise ValueError(f"插件目录含不支持的文件类型: {relative}")
    if len(entries) > _MAX_FILES:
        raise ValueError("插件目录文件数量超过限制")
    entries.sort()
    if len({entry.casefold() for entry in entries}) != len(entries):
        raise ValueError("插件目录含在目标文件系统上大小写冲突的路径")
    if ".boxteam-plugin.json" in entries:
        raise ValueError("包内不能包含安装器保留的元数据文件")
    return tuple(entries)


def _parse_manifest(raw: bytes) -> PluginPackageManifest:
    value = commentjson.loads(raw.decode("utf-8"))
    if not isinstance(value, dict):
        raise TypeError("plugin.jsonc 必须是对象")
    return PluginPackageManifest.model_validate(value)


def _validate_package_files(manifest: PluginPackageManifest, names: Iterable[str], read_file: Callable[[str], bytes]) -> None:
    available = set(names)
    for plugin in manifest.plugins:
        if plugin.runtime is not None:
            entrypoint = plugin.entrypoint
            if entrypoint is None or _declared_file(entrypoint[0], available, f"插件入口: {plugin.id}") is None:
                raise ValueError(f"插件入口文件不存在: {plugin.id}")
            if plugin.runtime == "node" and not entrypoint[0].endswith((".js", ".mjs", ".cjs")):
                raise ValueError(f"Node 插件入口必须是 JavaScript 文件: {plugin.id}")
            if plugin.runtime == "python" and not entrypoint[0].endswith(".py"):
                raise ValueError(f"Python 插件入口必须是 Python 文件: {plugin.id}")
        if plugin.runtime is not None:
            if plugin.runtime == "python" and ("requirements.txt" in available or "requirements.lock" in available or "uv.lock" in available or "poetry.lock" in available):
                raise ValueError("插件依赖准备尚未实现，拒绝发布包含 Python 依赖锁文件的代码插件")
            if plugin.runtime == "python" and "pyproject.toml" in available:
                project = tomllib.loads(read_file("pyproject.toml").decode("utf-8")).get("project", {})
                if project.get("dependencies") or project.get("optional-dependencies"):
                    raise ValueError("插件依赖准备尚未实现，拒绝发布声明 Python 依赖的代码插件")
            if plugin.runtime == "node" and "package.json" in available:
                package_json = json.loads(read_file("package.json").decode("utf-8"))
                if package_json.get("dependencies") or package_json.get("optionalDependencies"):
                    raise ValueError("插件依赖准备尚未实现，拒绝发布声明 Node 依赖的代码插件")
        for skill in plugin.contributes.skills:
            skill_path = skill.path
            if not skill_path:
                raise TypeError(f"Skill 目录路径无效: {plugin.id}")
            normalized_skill_path = _safe_member(skill_path)
            if normalized_skill_path != skill_path.rstrip("/"):
                raise ValueError(f"Skill 目录必须是包根内的规范相对路径: {plugin.id}")
            skill_file = f"{normalized_skill_path}/SKILL.md"
            if _declared_file(skill_file, available, f"Skill: {plugin.id}") is None:
                raise ValueError(f"插件 Skill 目录缺少 SKILL.md: {plugin.id}")
    _validate_skill_frontmatter(manifest, read_file)


def _declared_file(path: str, available: set[str], label: str) -> str | None:
    if _safe_member(path) != path:
        raise ValueError(f"{label} 必须是包根内的规范相对路径")
    if path not in available:
        return None
    return path


def _validate_skill_frontmatter(manifest: PluginPackageManifest, read_file: Callable[[str], bytes]) -> None:
    from app.services.business.resource_derivations.skill_frontmatter import (
        parse_skill_frontmatter,
    )

    names: set[tuple[str, str]] = set()
    for plugin in manifest.plugins:
        for skill in plugin.contributes.skills:
            directory = skill.path.rstrip("/")
            try:
                content = read_file(f"{directory}/SKILL.md").decode("utf-8")
            except UnicodeDecodeError as error:
                raise ValueError(f"Skill SKILL.md 必須是 UTF-8: {plugin.id}") from error
            key = (plugin.instance_scope, parse_skill_frontmatter(content).name)
            if key in names:
                raise ValueError(f"包内同一插件范围存在重复 Skill 名称: {key[1]}")
            names.add(key)


def _extract(archive: zipfile.ZipFile, entries: dict[str, zipfile.ZipInfo], destination: Path) -> None:
    for name, info in entries.items():
        target = destination.joinpath(*PurePosixPath(name).parts)
        if info.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        with archive.open(info) as source, target.open("xb") as output:
            shutil.copyfileobj(source, output)


def _copy_directory(source: Path, entries: tuple[str, ...], destination: Path) -> None:
    for name in entries:
        target = destination.joinpath(*PurePosixPath(name).parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source / name, target)


def _directory_digest(source: Path, entries: tuple[str, ...]) -> str:
    digest = hashlib.sha256()
    for name in entries:
        encoded = name.encode("utf-8")
        content = (source / name).read_bytes()
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


def _read_installed(path: Path) -> InstalledPluginPackage:
    if path.is_symlink() or path.parent.is_symlink() or not path.is_dir():
        raise FileNotFoundError(f"插件包版本不存在或不是普通目录: {path.name}")
    metadata_path = path / ".boxteam-plugin.json"
    if metadata_path.is_symlink():
        raise RuntimeError("已安装插件元数据不能是符号链接")
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    manifest = PluginPackageManifest.model_validate(metadata["manifest"])
    if path.name != manifest.version or path.parent.name != manifest.package_id:
        raise RuntimeError("已安装插件目录与 package_id/version 不一致")
    return InstalledPluginPackage(manifest=manifest, digest=metadata["digest"], root=path)


def _validate_identity(package_id: str, version: str) -> None:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", package_id):
        raise ValueError("package_id 不是有效包目录身份")
    if not _VERSION_PATTERN.fullmatch(version):
        raise ValueError("package version 不是有效目录身份")


def _sync_tree(root: Path) -> None:
    directories = [root]
    for current, child_dirs, files in os.walk(root):
        directory = Path(current)
        directories.extend(directory / name for name in child_dirs)
        for name in files:
            fsync_file(directory / name)
    for directory in reversed(directories):
        fsync_directory(directory)


__all__ = ["InstalledPluginPackage", "PluginPackageStore"]
