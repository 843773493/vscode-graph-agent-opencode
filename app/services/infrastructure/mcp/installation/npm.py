"""使用目标已有 Node／npm 准备标准 registry 的锁定 JavaScript 制品。"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from urllib.parse import urlsplit

from app.services.infrastructure.mcp.installation.runtime import (
    InstallationUnavailable,
    StageCallback,
    run_tool,
)


@dataclass(frozen=True, slots=True)
class NpmRuntime:
    node: Path
    cli: Path


def npm_runtime() -> NpmRuntime:
    configured = os.environ.get("BOXTEAM_NODE_BIN")
    executable = shutil.which(configured or "node")
    if executable is None:
        raise InstallationUnavailable("目标缺少已有 Node；请配置 BOXTEAM_NODE_BIN 或目标 PATH，安装器不会全局安装 runtime")
    node = Path(executable).resolve(strict=True)
    candidates = [node.parent / "node_modules/npm/bin/npm-cli.js",
                  node.parent.parent / "lib/node_modules/npm/bin/npm-cli.js"]
    npm = shutil.which("npm")
    if npm is not None:
        resolved = Path(npm).resolve(strict=True)
        candidates.append(resolved if resolved.name == "npm-cli.js" else resolved.parent / "node_modules/npm/bin/npm-cli.js")
    cli = next((path.resolve() for path in candidates if path.is_file()), None)
    if cli is None:
        raise InstallationUnavailable("目标已有 Node 缺少可用的标准 npm CLI；不会执行 npx 或全局安装包管理器")
    return NpmRuntime(node, cli)


def npm_package(parameters: dict[str, object]) -> tuple[str, str, str]:
    if parameters.get("runtime_hint") not in {None, "npm", "npx"} or parameters.get("runtime_arguments"):
        raise InstallationUnavailable("npm 制品只支持 npm／npx 提示及空 runtimeArguments，不执行动态启动参数")
    if parameters.get("expected_sha256") is not None:
        raise InstallationUnavailable("npm 制品使用 package-lock 的完整依赖 integrity，尚不支持目录单文件 fileSha256")
    name, version = parameters.get("identifier"), parameters.get("version")
    if not isinstance(name, str) or not name or not isinstance(version, str) or not version:
        raise TypeError("npm 制品必须指定标准包名与精确版本")
    index = parameters.get("registry_base_url") or "https://registry.npmjs.org"
    if not isinstance(index, str):
        raise TypeError("npm registryBaseUrl 必须为字符串")
    parsed = urlsplit(index)
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment or (parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"})):
        raise InstallationUnavailable("npm registry 必须是无凭据的明确 HTTPS 地址；HTTP 仅支持 loopback")
    return name, version, index.rstrip("/")


def npm_entrypoint(root: Path, *, name: str, version: str, entrypoint: str) -> Path:
    package = root / "node_modules" / name
    manifest = json.loads((package / "package.json").read_text(encoding="utf-8"))
    if (manifest.get("name"), manifest.get("version")) != (name, version):
        raise InstallationUnavailable("已安装 npm 根包的名称或精确版本与请求不一致")
    bins = manifest.get("bin", {})
    if isinstance(bins, str):
        bins = {name.rsplit("/", 1)[-1]: bins}
    if not isinstance(bins, dict) or not isinstance(bins.get(entrypoint), str):
        raise InstallationUnavailable("显式 npm entrypoint 不是根 package 的 bin，不使用依赖包或首个入口回退")
    command = (package / bins[entrypoint]).resolve(strict=True)
    if not command.is_relative_to(package.resolve()) or not command.is_file():
        raise InstallationUnavailable("npm 根 package bin 必须是包内的实际 JavaScript 文件")
    return command


async def prepare_npm_artifact(root: Path, *, name: str, version: str, index: str,
    entrypoint: str, runtime: NpmRuntime, progress: StageCallback,
    execution_directory: Path) -> tuple[str, str, dict[str, str], str]:
    root.mkdir(parents=True, exist_ok=False)
    environment = {key: value for key, value in os.environ.items()
                   if not key.upper().startswith(("NPM_", "NODE_"))}
    run = partial(run_tool, directory=execution_directory, progress=progress,
                  environment=environment, tool="Node／npm")
    # 版本与包名交给同一 npm 自带的解析器；URL、alias、范围和动态 tag 均不能成为受管根包。
    validate = ("import {createRequire} from 'node:module';const r=createRequire(process.argv[1]);"
        "const p=r('npm-package-arg').resolve(process.argv[2],process.argv[3]);"
        "if(p.type!=='version'||p.name!==process.argv[2]||r('semver').valid(process.argv[3])!==process.argv[3])"
        "throw Error('npm managed package requires an exact canonical version');"
        "console.log(JSON.stringify({node_version:process.version}));")
    metadata = json.loads(await run([str(runtime.node), "--input-type=module", "-e", validate, str(runtime.cli), name, version], stage="verify"))
    (root / "package.json").write_text(json.dumps({"name": "boxteam-mcp-artifact", "version": "0.0.0",
        "private": True, "dependencies": {name: version}}, indent=2) + "\n", encoding="utf-8")
    user_config, global_config = root / "user.npmrc", root / "global.npmrc"
    # 显式 prefix 是 npm 的 localPrefix；三层配置都属于本次制品，不能读取目标已有的 scoped registry。
    (root / ".npmrc").write_text("", encoding="utf-8")
    user_config.write_text("", encoding="utf-8")
    global_config.write_text("", encoding="utf-8")
    common = [str(runtime.node), str(runtime.cli), "--prefix", str(root), "--registry", index,
        "--userconfig", str(user_config), "--globalconfig", str(global_config),
        "--cache", str(execution_directory / "npm-cache"), "--ignore-scripts", "--no-audit", "--no-fund", "--engine-strict"]
    await run([*common, "install", "--package-lock-only"], stage="lock")
    lock_path = root / "package-lock.json"
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    if lock.get("lockfileVersion") != 3 or lock.get("packages", {}).get("", {}).get("dependencies") != {name: version}:
        raise InstallationUnavailable("npm 未生成包含精确根包的标准 v3 package-lock")
    for location, package in lock["packages"].items():
        if not location:
            continue
        resolved, integrity = package.get("resolved"), package.get("integrity")
        if (not location.startswith("node_modules/") or package.get("link") or not isinstance(resolved, str)
                or not resolved.startswith(index + "/") or not isinstance(integrity, str) or not integrity):
            raise InstallationUnavailable("npm 依赖锁包含所选 registry 之外的来源、链接或缺失 integrity，未发布制品")
    lock_bytes = lock_path.read_bytes()
    await run([*common, "ci"], stage="install")
    # 由同一生态工具核实完整依赖树；不能把缺失的 required/peer dependency 静默跳过。
    await run([*common, "ls", "--all", "--json"], stage="verify")
    if lock_path.read_bytes() != lock_bytes:
        raise InstallationUnavailable("npm ci 改变了已冻结的 package-lock，未发布制品")
    distributions: dict[str, str] = {}
    for location, package in lock["packages"].items():
        installed = root / location / "package.json"
        if not location or not installed.is_file():
            continue
        actual = json.loads(installed.read_text(encoding="utf-8"))
        if actual.get("version") != package.get("version"):
            raise InstallationUnavailable("npm 实际依赖版本与 package-lock 不一致")
        if package.get("hasInstallScript"):
            # TODO(2.3)：需要 install scripts 的构建制品须另行实现并验证，不能忽略构建后发布成功。
            raise InstallationUnavailable("npm 制品当前不执行依赖 install scripts；需要构建的包尚不支持受管安装")
        distributions[location.removeprefix("node_modules/")] = actual["version"]
    command = npm_entrypoint(root, name=name, version=version, entrypoint=entrypoint)
    await run([str(runtime.node), "--check", str(command)], stage="verify")
    return str(command), "sha256:" + hashlib.sha256(lock_bytes).hexdigest(), distributions, metadata["node_version"]
