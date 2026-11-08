"""按包内锁文件准备 Gateway 插件私有运行依赖。"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile
import tomllib
from pathlib import Path
from urllib.parse import urlsplit

from app.core.child_process import OwnedChildProcess
from app.core.identifier import create_uuid_hex
from app.core.shielded_cleanup import run_cleanup_shielded
from app.gateway.runtime.workspace_process import resolve_python_executable

_MAX_OUTPUT = 65536
_INSTALL_TIMEOUT = 600


def has_python_lock(root: Path) -> bool:
    return (root / "pyproject.toml").is_file() or (root / "uv.lock").is_file()


def has_node_lock(root: Path) -> bool:
    if (root / "package-lock.json").is_file():
        return True
    manifest = root / "package.json"
    if not manifest.is_file():
        return False
    package = json.loads(manifest.read_text(encoding="utf-8"))
    return any(package.get(field) for field in ("dependencies", "optionalDependencies", "peerDependencies"))


def prepare_dependencies(root: Path, *, runtimes: set[str]) -> None:
    if "python" in runtimes and has_python_lock(root):
        asyncio.run(_prepare_python(root))
    if "node" in runtimes and has_node_lock(root):
        asyncio.run(_prepare_node(root))


def validate_dependency_inputs(root: Path, *, runtimes: set[str]) -> None:
    python_project = root / "pyproject.toml"
    python_lock = root / "uv.lock"
    if has_python_lock(root):
        if "python" not in runtimes:
            raise ValueError("仅 Python 代码插件可包含 pyproject.toml 或 uv.lock")
        if not python_project.is_file() or not python_lock.is_file():
            raise ValueError("Python 插件依赖必须同时提供 pyproject.toml 与 uv.lock")
        _validate_python_lock(python_project, python_lock)
    for unsupported in ("requirements.txt", "requirements.lock", "poetry.lock", "Pipfile", "Pipfile.lock"):
        if (root / unsupported).exists():
            raise ValueError(f"Python 插件不支持 {unsupported}；请提供 pyproject.toml 与 uv.lock")
    node_manifest, node_lock = root / "package.json", root / "package-lock.json"
    if node_manifest.is_file():
        package = json.loads(node_manifest.read_text(encoding="utf-8"))
        dependencies = any(package.get(field) for field in ("dependencies", "optionalDependencies", "peerDependencies"))
        if dependencies and ("node" not in runtimes or not node_lock.is_file()):
            raise ValueError("声明 Node 运行依赖时必须由 Node 代码插件同时提供 package-lock.json")
    if node_lock.is_file():
        if "node" not in runtimes or not node_manifest.is_file():
            raise ValueError("仅 Node 代码插件可包含 package-lock.json，且必须同时提供 package.json")
        _validate_node_lock(node_manifest, node_lock)


def _validate_python_lock(project_file: Path, lock_file: Path) -> None:
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


def _validate_node_lock(manifest_path: Path, lock_path: Path) -> None:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    if lock.get("lockfileVersion") != 3 or not isinstance(lock.get("packages", {}).get(""), dict):
        raise ValueError("Node 插件仅支持标准 npm package-lock v3")
    root_package = lock["packages"][""]
    for dependency_field in ("dependencies", "optionalDependencies", "devDependencies"):
        if manifest.get(dependency_field, {}) != root_package.get(dependency_field, {}):
            raise ValueError(f"Node package-lock 根 {dependency_field} 与 package.json 不一致")
    for location, package in lock["packages"].items():
        if not location:
            continue
        resolved = package.get("resolved")
        parsed = urlsplit(resolved) if isinstance(resolved, str) else None
        loopback_http = parsed is not None and parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
        if (not location.startswith("node_modules/") or package.get("link")
                or parsed is None or (parsed.scheme != "https" and not loopback_http)
                or not parsed.hostname or parsed.username or parsed.password or not package.get("integrity")):
            raise ValueError("Node 插件锁文件包含非 HTTPS、链接或缺少 integrity 的依赖")
        if package.get("hasInstallScript") and location != "node_modules/node-pty":
            raise ValueError("Node 插件依赖需要 install hook，但仅支持内建 node-pty 工具链准备")


async def _run(command: list[str], *, cwd: Path, env: dict[str, str], stage: str) -> str:
    process = OwnedChildProcess(create_uuid_hex(namespace="gateway-plugin-dependency"))
    output = bytearray()

    async def collect() -> None:
        child = process.process
        assert child is not None and child.stdout is not None
        async for chunk in child.stdout:
            if len(output) < _MAX_OUTPUT:
                output.extend(chunk[:_MAX_OUTPUT - len(output)])
        await child.wait()

    try:
        async with asyncio.timeout(_INSTALL_TIMEOUT):
            await process.start(command, cwd=cwd, env=env, stderr=-2)
            await collect()
    finally:
        async def close() -> None:
            await process.close()

        await run_cleanup_shielded(close)
    if process.state.returncode != 0:
        detail = bytes(output).decode("utf-8", errors="replace")
        raise RuntimeError(f"插件依赖阶段失败: stage={stage}, exit_code={process.state.returncode}: {detail}")
    return bytes(output).decode("utf-8", errors="replace")


def _python_environment() -> dict[str, str]:
    return {
        key: value
        for key, value in os.environ.items()
        if not key.upper().startswith(("UV_", "PIP_", "PYTHON"))
        and key not in {"VIRTUAL_ENV", "CONDA_PREFIX"}
    }


async def _prepare_python(root: Path) -> None:
    uv = shutil.which("uv")
    if uv is None:
        raise RuntimeError("准备 Python 插件依赖需要目标已有 uv")
    command_prefix = [uv, "--no-config", "--no-cache", "--project", str(root)]
    options = ["--python", str(resolve_python_executable()), "--no-python-downloads", "--no-managed-python", "--no-build"]
    await _run([*command_prefix, "sync", "--locked", "--no-dev", "--no-default-groups",
        "--no-install-project", "--link-mode", "copy", *options], cwd=root,
        env=_python_environment(), stage="python-sync")


def _npm_runtime() -> tuple[Path, Path]:
    configured = os.environ.get("BOXTEAM_NODE_BIN")
    if not configured:
        raise RuntimeError("准备 Node 插件依赖需要明确配置 BOXTEAM_NODE_BIN")
    node_path = shutil.which(configured)
    if node_path is None:
        raise RuntimeError("准备 Node 插件依赖需要目标已有 Node")
    node = Path(node_path).resolve(strict=True)
    npm = shutil.which("npm")
    candidates = [node.parent / "node_modules/npm/bin/npm-cli.js", node.parent.parent / "lib/node_modules/npm/bin/npm-cli.js"]
    if npm is not None:
        npm_path = Path(npm).resolve(strict=True)
        candidates.append(npm_path if npm_path.name == "npm-cli.js" else npm_path.parent / "node_modules/npm/bin/npm-cli.js")
    cli = next((candidate.resolve() for candidate in candidates if candidate.is_file()), None)
    if cli is None:
        raise RuntimeError("目标已有 Node 缺少标准 npm CLI")
    return node, cli


def _node_headers_root(node: Path) -> Path:
    for root in (node.parent, node.parent.parent):
        if (root / "include/node/node.h").is_file():
            return root
    raise RuntimeError("目标 Node 未提供本机 headers，不能离线构建 node-pty")


async def _prepare_node(root: Path) -> None:
    lock_path = root / "package-lock.json"
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    node, npm_cli = _npm_runtime()
    environment = {key: value for key, value in os.environ.items()
        if not key.upper().startswith(("NPM_", "NODE_"))}
    before = lock_path.read_bytes()
    with tempfile.TemporaryDirectory(prefix=".npm-prepare-", dir=root.parent) as temporary:
        configuration = Path(temporary)
        empty_user, empty_global = configuration / "user.npmrc", configuration / "global.npmrc"
        empty_user.touch()
        empty_global.touch()
        command = [str(node), str(npm_cli), "--prefix", str(root), "--userconfig", str(empty_user),
            "--globalconfig", str(empty_global), "--cache", str(configuration / "cache"),
            "--ignore-scripts", "--no-audit", "--no-fund", "ci"]
        await _run(command, cwd=root, env=environment, stage="npm-ci")
    if lock_path.read_bytes() != before:
        raise RuntimeError("npm ci 改写了锁文件；插件包未发布")
    if any(package.get("hasInstallScript") for location, package in lock["packages"].items()
        if location and package.get("hasInstallScript")):
        await _build_node_pty(root, node, npm_cli, environment)


async def _build_node_pty(root: Path, node: Path, npm_cli: Path, environment: dict[str, str]) -> None:
    package_root = root / "node_modules/node-pty"
    if not (package_root / "binding.gyp").is_file():
        raise ValueError("node-pty 缺少 binding.gyp，拒绝运行包提供的安装脚本")
    npm_root = npm_cli.parent.parent
    node_gyp = npm_root / "node_modules/node-gyp/bin/node-gyp.js"
    if not node_gyp.is_file():
        raise RuntimeError("目标 npm 未携带 node-gyp，不能构建 node-pty 原生依赖")
    node_root = _node_headers_root(node)
    python = str(resolve_python_executable())
    await _run([str(node), str(node_gyp), "rebuild", f"--nodedir={node_root}"], cwd=package_root,
        env={**environment, "PYTHON": python}, stage="node-pty-native-build")

