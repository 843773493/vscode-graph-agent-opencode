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

import commentjson

from app.core.child_process import OwnedChildProcess
from app.core.identifier import create_uuid_hex
from app.core.shielded_cleanup import run_cleanup_shielded
from app.gateway.runtime.workspace_process import resolve_python_executable

_MAX_OUTPUT = 65536
_INSTALL_TIMEOUT = 600


def has_python_lock(root: Path) -> bool:
    return (root / "pyproject.toml").is_file() or (root / "uv.lock").is_file()


def has_node_lock(root: Path) -> bool:
    if (root / "bun.lock").is_file():
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
    for obsolete in ("package-lock.json", "npm-shrinkwrap.json", "yarn.lock", "bun.lockb"):
        if (root / obsolete).exists():
            raise ValueError(f"Node 插件不支持 {obsolete}；请提供 package.json 与 bun.lock")
    node_manifest, node_lock = root / "package.json", root / "bun.lock"
    if node_manifest.is_file():
        package = json.loads(node_manifest.read_text(encoding="utf-8"))
        dependencies = any(package.get(field) for field in ("dependencies", "optionalDependencies", "peerDependencies"))
        if dependencies and ("node" not in runtimes or not node_lock.is_file()):
            raise ValueError("声明 Node 运行依赖时必须由 Node 代码插件同时提供 bun.lock")
    if node_lock.is_file():
        if "node" not in runtimes or not node_manifest.is_file():
            raise ValueError("仅 Node 代码插件可包含 bun.lock，且必须同时提供 package.json")
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
    lock = commentjson.loads(lock_path.read_text(encoding="utf-8"))
    if lock.get("lockfileVersion") != 1 or not isinstance(lock.get("workspaces", {}).get(""), dict):
        raise ValueError("Node 插件仅支持标准 Bun 文本锁文件 v1")
    if set(lock["workspaces"]) != {""}:
        raise ValueError("插件包必须有单一根 package.json，不支持 workspace 链接")
    root_package = lock["workspaces"][""]
    for dependency_field in ("dependencies", "optionalDependencies", "devDependencies", "peerDependencies"):
        if manifest.get(dependency_field, {}) != root_package.get(dependency_field, {}):
            raise ValueError(f"Bun 锁文件根 {dependency_field} 与 package.json 不一致")
    for package in lock.get("packages", {}).values():
        if not isinstance(package, list) or len(package) != 4 or not isinstance(package[0], str):
            raise ValueError("Bun 锁文件只允许带完整性摘要的 registry 依赖")
        name, separator, version = package[0].rpartition("@")
        source = package[1]
        if not separator or not name or not version or not isinstance(source, str) or not isinstance(package[3], str) or not package[3].startswith("sha512-"):
            raise ValueError("Bun 锁文件依赖缺少明确版本或 integrity")
        # Bun 的默认 registry 用空字符串表示；显式 tarball／registry 只允许 HTTPS 或 loopback。
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
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.upper().startswith(("UV_", "PIP_", "PYTHON"))
        and key not in {"VIRTUAL_ENV", "CONDA_PREFIX"}
    }
    if "UV_CACHE_DIR" in os.environ:
        environment["UV_CACHE_DIR"] = os.environ["UV_CACHE_DIR"]
    return environment


async def _prepare_python(root: Path) -> None:
    uv = _installation_tool("BOXTEAM_UV_BIN", "uv")
    command_prefix = [uv, "--no-config", "--project", str(root)]
    options = ["--python", str(resolve_python_executable()), "--no-python-downloads", "--no-managed-python", "--no-build"]
    await _run([*command_prefix, "sync", "--locked", "--no-dev", "--no-default-groups",
        "--no-install-project", "--link-mode", "copy", *options], cwd=root,
        env=_python_environment(), stage="python-sync")


def _installation_tool(variable: str, source_command: str) -> str:
    configured = os.environ.get(variable)
    if not configured and not os.environ.get("BOXTEAM_RUNTIME_MANIFEST"):
        configured = shutil.which(source_command)
    if not configured or not Path(configured).is_absolute() or not Path(configured).is_file():
        raise RuntimeError(f"插件安装需要目标运行时提供明确的 {variable}")
    return str(Path(configured).resolve(strict=True))


def _node_headers_root(node: Path) -> Path | None:
    for root in (node.parent, node.parent.parent):
        if (root / "include/node/node.h").is_file():
            return root
    return None


async def _prepare_node(root: Path) -> None:
    lock_path = root / "bun.lock"
    bun = _installation_tool("BOXTEAM_BUN_BIN", "bun")
    environment = {key: value for key, value in os.environ.items()
        if not key.upper().startswith(("NPM_", "NODE_", "BUN_"))}
    # 保留正常缓存位置，隔离认证与配置；不再为每次安装创建空缓存。
    environment["BUN_INSTALL_CACHE_DIR"] = os.environ.get("BUN_INSTALL_CACHE_DIR", str(Path.home() / ".bun/install/cache"))
    for key in ("XDG_CACHE_HOME",):
        if key in os.environ:
            environment[key] = os.environ[key]
    before = lock_path.read_bytes()
    with tempfile.TemporaryDirectory(prefix=".bun-prepare-", dir=root.parent) as temporary:
        configuration = Path(temporary)
        install_environment = dict(environment)
        for key in ("HOME", "USERPROFILE", "XDG_CONFIG_HOME"):
            install_environment[key] = str(configuration)
        empty_config = configuration / "bunfig.toml"
        empty_config.touch()
        for key in ("NPM_CONFIG_USERCONFIG", "NPM_CONFIG_GLOBALCONFIG"):
            file = configuration / (key.lower() + ".npmrc")
            file.touch()
            install_environment[key] = str(file)
        await _run([bun, "--no-env-file", f"--config={empty_config}", "install",
            "--frozen-lockfile", "--production", "--ignore-scripts", "--backend", "copyfile"],
            cwd=root, env=install_environment, stage="bun-install")
    if lock_path.read_bytes() != before:
        raise RuntimeError("Bun 安装改写了锁文件；插件包未发布")
    if (root / "node_modules/node-pty/package.json").is_file():
        await _build_node_pty(root, environment)


async def _build_node_pty(root: Path, environment: dict[str, str]) -> None:
    package_root = root / "node_modules/node-pty"
    if not (package_root / "binding.gyp").is_file():
        raise ValueError("node-pty 缺少 binding.gyp，拒绝运行包提供的安装脚本")
    node = Path(_installation_tool("BOXTEAM_NODE_BIN", "node"))
    configured_gyp = os.environ.get("BOXTEAM_NODE_GYP_JS")
    if not configured_gyp and os.environ.get("BOXTEAM_RUNTIME_MANIFEST"):
        raise RuntimeError("目标运行时未提供 BOXTEAM_NODE_GYP_JS")
    node_gyp = Path(configured_gyp) if configured_gyp else Path.cwd() / "node_modules/node-gyp/bin/node-gyp.js"
    if not node_gyp.is_absolute() or not node_gyp.is_file():
        raise RuntimeError("目标运行时缺少明确的 BOXTEAM_NODE_GYP_JS，不能构建 node-pty")
    command = [str(node), str(node_gyp), "rebuild", f"--python={resolve_python_executable()}"]
    # 同一 Node 发行版的 headers 优先复用；缺少时 node-gyp 按执行它的目标 Node 版本取标准缓存。
    node_root = _node_headers_root(node)
    if node_root is not None:
        command.append(f"--nodedir={node_root}")
    await _run(command, cwd=package_root, env=environment, stage="node-pty-native-build")
