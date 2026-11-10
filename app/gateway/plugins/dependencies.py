"""校验插件制品包含完整、可重定位的运行依赖。"""

from __future__ import annotations

import json
import platform
import subprocess
import sys
import tomllib
from email.parser import Parser
from functools import cache
from pathlib import Path, PureWindowsPath
from urllib.parse import urlsplit

import commentjson
from packaging.requirements import Requirement
from packaging.utils import NormalizedName, canonicalize_name

from app.schemas.gateway.service_plugin import PluginPackageTarget

_VALIDATED_PACKAGES: set[tuple[str, tuple[str, ...]]] = set()


def validate_runtime_target(target: PluginPackageTarget | None, *, runtimes: set[str], node_executable: str | None,
                            python_executable: Path) -> None:
    if target is None:
        raise ValueError("代码插件包缺少目标运行环境")
    target_os = target.os
    current_os = {"linux": "linux", "win32": "windows", "darwin": "macos"}.get(sys.platform)
    if current_os != target_os:
        raise ValueError(f"插件目标操作系统不匹配: package={target_os}, gateway={current_os or sys.platform}；"
                           "请获取目标 Gateway 操作系统对应的完整插件 ZIP")
    architecture = {"AMD64": "x64", "x86_64": "x64", "aarch64": "arm64", "ARM64": "arm64"}.get(platform.machine())
    if architecture != target.architecture:
        raise ValueError(f"插件目标架构不匹配: package={target.architecture}, gateway={architecture or platform.machine()}；"
                           "请获取目标 Gateway 架构对应的完整插件 ZIP")
    _validate_system_baseline(target.system_baseline, target_os)
    if "node" in runtimes:
        runtime = target.node
        if runtime is None:
            raise ValueError("插件包缺少 Node 目标运行时")
        if node_executable is None:
            raise ValueError("Node 插件没有配置目标 Node 运行时")
        node = _node_runtime(node_executable)
        if node["version"] != runtime.version or node["abi"] != runtime.abi:
            raise ValueError(
                f"插件 Node 运行时不匹配: package={runtime.version}/{runtime.abi}, gateway={node['version']}/{node['abi']}；"
                "请获取匹配目标应用 Node 版本和 ABI 的完整插件 ZIP，或安装匹配的应用版本；不会现场编译"
            )
    if "python" in runtimes:
        if target.python is None:
            raise ValueError("插件包缺少 Python 目标运行时")
        runtime = _python_runtime(python_executable)
        if runtime["version"] != target.python.version or runtime["abi"] != target.python.abi:
            raise ValueError(
                f"插件 Python 运行时不匹配: package={target.python.version}/{target.python.abi}, "
                f"gateway={runtime['version']}/{runtime['abi']}；"
                "请获取匹配目标应用 Python 版本和 ABI 的完整插件 ZIP，或安装匹配的应用版本"
            )


def _validate_system_baseline(baseline: str, target_os: str) -> None:
    if target_os == "linux":
        label, actual = "glibc", platform.libc_ver()
        if actual[0] != "glibc":
            raise ValueError(f"插件需要 glibc，当前系统 libc 为 {actual[0] or 'unknown'}")
        prefix = "glibc-"
    elif target_os == "windows":
        if sys.platform != "win32":
            raise RuntimeError("Windows 插件基线只能在 Windows Gateway 上校验")
        version = sys.getwindowsversion()
        label, actual = "windows", (f"{version.major}.{version.minor}.{version.build}", "")
        prefix = "windows-"
    else:
        label, actual = "macos", (platform.mac_ver()[0], "")
        prefix = "macos-"
    if not baseline.startswith(prefix):
        raise ValueError(f"插件目标系统基线格式无效: {baseline}")
    required = _version_tuple(baseline.removeprefix(prefix))
    current = _version_tuple(actual[1] if target_os == "linux" else actual[0])
    if current < required:
        raise ValueError(f"插件系统基线不满足: required={label} {baseline.removeprefix(prefix)}, "
                           f"gateway={actual[1] if target_os == 'linux' else actual[0]}；"
                           "请获取兼容目标系统基线的完整插件 ZIP，或在满足基线的系统运行")


def _version_tuple(value: str) -> tuple[int, ...]:
    try:
        return tuple(int(part) for part in value.split("."))
    except ValueError as error:
        raise ValueError(f"无效的系统版本号: {value}") from error


@cache
def _node_runtime(executable: str) -> dict[str, str]:
    result = subprocess.run(
        [executable, "-p", "JSON.stringify({version:process.versions.node,abi:process.versions.modules})"],
        capture_output=True, text=True, timeout=5, check=True,
    )
    value = json.loads(result.stdout)
    if not isinstance(value, dict) or not isinstance(value.get("version"), str) or not isinstance(value.get("abi"), str):
        raise TypeError("目标 Node 返回了无效的运行时元数据")
    return value


@cache
def _python_runtime(executable: Path) -> dict[str, str]:
    result = subprocess.run(
        [str(executable), "-c", "import json,sys; print(json.dumps({'version':sys.version.split()[0], 'abi':f'cp{sys.version_info.major}{sys.version_info.minor}'}))"],
        capture_output=True, text=True, timeout=5, check=True,
    )
    value = json.loads(result.stdout)
    if not isinstance(value, dict) or not isinstance(value.get("version"), str) or not isinstance(value.get("abi"), str):
        raise TypeError("目标 Python 返回了无效的运行时元数据")
    return value


def validate_prebuilt_dependencies(root: Path, *, runtimes: set[str], digest: str | None = None) -> None:
    cache_key = (digest, tuple(sorted(runtimes))) if digest is not None else None
    if cache_key is not None and cache_key in _VALIDATED_PACKAGES:
        return
    if (root / ".venv").exists():
        raise ValueError("插件包不能携带构建机 Python .venv；请提供可重定位的 python/site-packages")
    python_project = root / "pyproject.toml"
    python_lock = root / "uv.lock"
    python_site = root / "python/site-packages"
    if python_project.exists() or python_lock.exists():
        if "python" not in runtimes:
            raise ValueError("仅 Python 代码插件可包含 pyproject.toml 或 uv.lock")
        if not python_project.is_file() or not python_lock.is_file():
            raise ValueError("Python 插件依赖必须同时提供 pyproject.toml 与 uv.lock")
        requirements = _validate_python_lock(python_project, python_lock)
        if requirements and not python_site.is_dir():
            raise ValueError("Python 插件缺少已准备的 python/site-packages")
        if python_site.exists():
            _validate_python_site(root, python_site, requirements)
    elif python_site.exists():
        raise ValueError("Python site-packages 必须由同包 pyproject.toml 与 uv.lock 声明")

    for unsupported in ("requirements.txt", "requirements.lock", "poetry.lock", "Pipfile", "Pipfile.lock"):
        if (root / unsupported).exists():
            raise ValueError(f"Python 插件不支持 {unsupported}；请提供 pyproject.toml 与 uv.lock")

    for obsolete in ("package-lock.json", "npm-shrinkwrap.json", "yarn.lock", "bun.lockb"):
        if (root / obsolete).exists():
            raise ValueError(f"Node 插件不支持 {obsolete}；请提供 package.json 与 bun.lock")
    node_manifest, node_lock = root / "package.json", root / "bun.lock"
    node_modules = root / "node_modules"
    if node_manifest.is_file():
        manifest = json.loads(node_manifest.read_text(encoding="utf-8"))
        locked_packages = _validate_node_lock(node_manifest, node_lock) if node_lock.is_file() else {}
        declared = _node_requirements(manifest)
        if declared and "node" not in runtimes:
            raise ValueError("仅 Node 代码插件可包含 Node 运行依赖")
        if declared and not node_lock.is_file():
            raise ValueError("声明 Node 运行依赖时必须同时提供 bun.lock")
        if declared and not node_modules.is_dir():
            raise ValueError("Node 插件缺少已准备的 node_modules")
        if not declared.issubset(locked_packages):
            raise ValueError("Bun 锁文件缺少 package.json 声明的依赖")
        if node_modules.exists():
            _validate_node_modules(root, manifest, locked_packages)
    elif node_lock.exists() or node_modules.exists():
        raise ValueError("Node bun.lock 与 node_modules 必须由同包 package.json 声明")
    if cache_key is not None:
        _VALIDATED_PACKAGES.add(cache_key)


def _node_requirements(manifest: dict[str, object]) -> set[str]:
    requirements: set[str] = set()
    for field in ("dependencies", "optionalDependencies", "peerDependencies"):
        values = manifest.get(field, {})
        if not isinstance(values, dict):
            raise TypeError(f"Node package.json {field} 必须是对象")
        requirements.update(name for name in values if isinstance(name, str))
    return requirements


def _validate_python_lock(project_file: Path, lock_file: Path) -> list[Requirement]:
    project = tomllib.loads(project_file.read_text(encoding="utf-8"))
    project_config = project.get("project")
    if not isinstance(project_config, dict):
        raise TypeError("Python 插件 pyproject.toml 必须声明标准 [project]")
    if project_config.get("optional-dependencies") or project_config.get("dependency-groups"):
        raise ValueError("Python 插件暂不支持 optional/dependency groups；请将运行依赖放入 [project].dependencies")
    lock = tomllib.loads(lock_file.read_text(encoding="utf-8"))
    if lock.get("version") != 1 or not isinstance(lock.get("package"), list):
        raise ValueError("Python 插件仅支持标准 uv.lock v1")
    for package in lock["package"]:
        if not isinstance(package, dict):
            raise TypeError("Python 插件 uv.lock package 条目格式无效")
        source = package.get("source", {})
        if source == {"virtual": "."}:
            continue
        registry = source.get("registry") if isinstance(source, dict) else None
        if not isinstance(registry, str):
            raise TypeError("Python 插件锁文件仅支持 registry wheel，不支持路径、Git 或其它来源")
        parsed = urlsplit(registry)
        if (parsed.scheme not in {"https", "http"} or not parsed.hostname or parsed.username or parsed.password
                or parsed.query or parsed.fragment
                or (parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"})):
            raise ValueError("Python 插件锁定 registry 必须是 HTTPS；HTTP 仅允许 loopback")
    dependencies = project_config.get("dependencies", [])
    if not isinstance(dependencies, list) or not all(isinstance(item, str) for item in dependencies):
        raise TypeError("Python 插件 [project].dependencies 必须是字符串数组")
    declared = [Requirement(item) for item in dependencies]
    active = [requirement for requirement in declared
              if requirement.marker is None or requirement.marker.evaluate()]
    project_package = next((package for package in lock["package"]
                            if isinstance(package, dict) and package.get("source") == {"virtual": "."}), None)
    if project_package is None:
        raise ValueError("Python 插件 uv.lock 缺少根项目记录")
    metadata = project_package.get("metadata", {})
    locked_requirements = metadata.get("requires-dist", []) if isinstance(metadata, dict) else []
    locked = [Requirement(_locked_requirement(item)) for item in locked_requirements]
    key = lambda requirement: (canonicalize_name(requirement.name), str(requirement.specifier),
        tuple(sorted(requirement.extras)), str(requirement.marker or ""))
    if sorted(map(key, active)) != sorted(map(key, locked)):
        raise ValueError("Python 插件 pyproject.toml 与 uv.lock 根依赖不一致")
    return active


def _locked_requirement(value: object) -> str:
    if not isinstance(value, dict) or not isinstance(value.get("name"), str):
        raise TypeError("Python 插件 uv.lock 根依赖条目格式无效")
    name = value["name"]
    specifier = value.get("specifier", "")
    if not isinstance(specifier, str):
        raise TypeError("Python 插件 uv.lock 根依赖版本格式无效")
    extras = value.get("extra", [])
    if not isinstance(extras, list) or not all(isinstance(extra, str) for extra in extras):
        raise TypeError("Python 插件 uv.lock 根依赖 extras 格式无效")
    marker = value.get("marker")
    if marker is not None and not isinstance(marker, str):
        raise TypeError("Python 插件 uv.lock 根依赖 marker 格式无效")
    return f"{name}[{','.join(extras)}]{specifier}" + (f" ; {marker}" if marker else "")


def _validate_node_lock(manifest_path: Path, lock_path: Path) -> dict[str, set[tuple[str, str]]]:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    lock = commentjson.loads(lock_path.read_text(encoding="utf-8"))
    if lock.get("lockfileVersion") != 1 or not isinstance(lock.get("workspaces", {}).get(""), dict):
        raise ValueError("Node 插件仅支持标准 Bun 文本锁文件 v1")
    if set(lock["workspaces"]) != {""}:
        raise ValueError("插件包必须有单一根 package.json，不支持 workspace 链接")
    root_package = lock["workspaces"][""]
    for dependency_field in ("dependencies", "optionalDependencies", "peerDependencies"):
        if manifest.get(dependency_field, {}) != root_package.get(dependency_field, {}):
            raise ValueError(f"Bun 锁文件根 {dependency_field} 与 package.json 不一致")
    packages = lock.get("packages", {})
    if not isinstance(packages, dict):
        raise TypeError("Bun 锁文件 packages 必须是对象")
    packages_by_reference: dict[str, set[tuple[str, str]]] = {}
    for key, package in packages.items():
        if not isinstance(package, list) or len(package) != 4 or not isinstance(package[0], str):
            raise ValueError("Bun 锁文件只允许带完整性摘要的 registry 依赖")
        name, separator, version = package[0].rpartition("@")
        source = package[1]
        if not separator or not name or not version or not isinstance(source, str) or not isinstance(package[3], str) or not package[3].startswith("sha512-"):
            raise ValueError("Bun 锁文件依赖缺少明确版本或 integrity")
        for location in (version, source):
            if not location:
                continue
            parsed = urlsplit(location)
            if parsed.scheme:
                if (parsed.scheme not in {"https", "http"} or not parsed.hostname or parsed.username or parsed.password
                        or (parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"})):
                    raise ValueError("Bun 锁文件不支持路径、Git、链接或携带凭据的依赖")
            elif "/" in location or location.startswith((".", "workspace:", "file:", "link:")):
                raise ValueError("Bun 锁文件不支持本机路径或 workspace 依赖")
        if not isinstance(key, str):
            raise TypeError("Bun 锁文件 package key 必须是字符串")
        package_identity = (name, version)
        packages_by_reference.setdefault(key, set()).add(package_identity)
        packages_by_reference.setdefault(name, set()).add(package_identity)
    return packages_by_reference


def _node_dependency_sets(manifest: dict[str, object]) -> tuple[set[str], set[str]]:
    required: set[str] = set()
    optional: set[str] = set()
    for field in ("dependencies", "peerDependencies", "optionalDependencies"):
        values = manifest.get(field, {})
        if not isinstance(values, dict) or not all(isinstance(name, str) for name in values):
            raise TypeError(f"Node package.json {field} 必须是对象且使用字符串键")
        names = set(values)
        if field == "optionalDependencies":
            optional.update(names)
        else:
            required.update(names)

    peer_metadata = manifest.get("peerDependenciesMeta", {})
    if not isinstance(peer_metadata, dict):
        raise TypeError("Node package.json peerDependenciesMeta 必须是对象")
    optional_peers = {
        name for name, value in peer_metadata.items()
        if isinstance(name, str) and isinstance(value, dict) and value.get("optional") is True
    }
    optional.update(required & optional_peers)
    required.difference_update(optional)
    return required, optional


def _resolve_node_package(root: Path, owner: Path, name: str) -> Path | None:
    parts = name.split("/")
    if parts[0].startswith("@"):
        if len(parts) != 2:
            raise ValueError(f"Node package.json 依赖名称无效: {name}")
    elif len(parts) != 1:
        raise ValueError(f"Node package.json 依赖名称无效: {name}")
    if any(part in {"", ".", ".."} or "\\" in part or ":" in part for part in parts):
        raise ValueError(f"Node package.json 依赖名称无效: {name}")

    current = owner
    while current == root or root in current.parents:
        package = current / "node_modules" / Path(*parts)
        if package.exists():
            return package
        if current == root:
            break
        current = current.parent
    return None


def _validate_node_modules(root: Path, manifest: dict[str, object],
                           locked_packages: dict[str, set[tuple[str, str]]]) -> None:
    required, optional = _node_dependency_sets(manifest)
    pending: list[tuple[Path, str, bool]] = []
    pending.extend((root, name, True) for name in required)
    pending.extend((root, name, False) for name in optional)
    visited: set[Path] = set()
    while pending:
        owner, name, is_required = pending.pop()
        package = _resolve_node_package(root, owner, name)
        if package is None:
            if is_required:
                raise ValueError(f"Node 插件依赖未准备完整: {name}")
            continue
        package_file = package / "package.json"
        if not package_file.is_file():
            raise ValueError(f"Node 插件依赖缺少 package.json: {name}")
        if package in visited:
            continue
        visited.add(package)
        installed = json.loads(package_file.read_text(encoding="utf-8"))
        if not isinstance(installed, dict):
            raise TypeError(f"Node 插件依赖 package.json 必须是对象: {name}")
        installed_name, installed_version = installed.get("name"), installed.get("version")
        locked_packages_for_dependency = locked_packages.get(name, set())
        if (not isinstance(installed_name, str) or not isinstance(installed_version, str)
                or (installed_name, installed_version) not in locked_packages_for_dependency):
            raise ValueError(f"Node 插件依赖版本与 bun.lock 不一致: {name}")
        dependencies, optional_dependencies = _node_dependency_sets(installed)
        pending.extend((package, dependency, True) for dependency in dependencies)
        pending.extend((package, dependency, False) for dependency in optional_dependencies)


def _validate_python_site(root: Path, site: Path, requirements: list[Requirement]) -> None:
    entries: dict[NormalizedName, tuple[Path, str]] = {}
    for path in site.glob("*.dist-info"):
        metadata_path = path / "METADATA"
        if not path.is_dir() or not metadata_path.is_file():
            raise ValueError(f"Python 插件包含不完整的 dist-info: {path.relative_to(root)}")
        metadata = Parser().parsestr(metadata_path.read_text(encoding="utf-8"))
        name, version = metadata.get("Name"), metadata.get("Version")
        if not name or not version:
            raise ValueError(f"Python 插件 METADATA 缺少 Name 或 Version: {metadata_path.relative_to(root)}")
        entries[canonicalize_name(name)] = (path, version)

    lock = tomllib.loads((root / "uv.lock").read_text(encoding="utf-8"))
    packages: dict[NormalizedName, dict[str, object]] = {
        canonicalize_name(item["name"]): item for item in lock["package"]
        if isinstance(item, dict) and isinstance(item.get("name"), str)
    }
    pending = [(canonicalize_name(requirement.name), frozenset(requirement.extras))
               for requirement in requirements]
    visited: set[tuple[NormalizedName, frozenset[str]]] = set()
    while pending:
        name, extras = pending.pop()
        key = (name, extras)
        if key in visited:
            continue
        visited.add(key)
        locked, installed = packages.get(name), entries.get(name)
        if locked is None or installed is None:
            raise ValueError(f"Python 插件依赖未准备完整: {name}")
        if installed[1] != locked.get("version"):
            raise ValueError(f"Python 插件依赖版本与 uv.lock 不一致: {name}")
        metadata = Parser().parsestr((installed[0] / "METADATA").read_text(encoding="utf-8"))
        for raw in metadata.get_all("Requires-Dist", []):
            requirement = Requirement(raw)
            marker_extras = extras or frozenset({""})
            if requirement.marker is None or any(
                requirement.marker.evaluate({"extra": extra}) for extra in marker_extras
            ):
                dependency = canonicalize_name(requirement.name)
                dependency_lock = packages.get(dependency)
                if dependency_lock is None:
                    raise ValueError(f"Python 插件依赖未锁定: {dependency}")
                if not requirement.specifier.contains(str(dependency_lock.get("version")), prereleases=True):
                    raise ValueError(f"Python 插件依赖版本与 wheel metadata 不一致: {dependency}")
                pending.append((dependency, frozenset(requirement.extras)))
    for metadata in site.rglob("*.pth"):
        for line in metadata.read_text(encoding="utf-8").splitlines():
            value = line.strip()
            if not value or value.startswith("import "):
                continue
            if Path(value).is_absolute() or PureWindowsPath(value).is_absolute():
                raise ValueError(f"Python 插件包含不可重定位的 .pth 路径: {metadata.relative_to(root)}")
