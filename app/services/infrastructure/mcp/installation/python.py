"""使用 uv 的标准锁文件和 wheel 安装；这里不实现依赖解析或包管理器。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import sys
import tomllib
from collections.abc import Awaitable, Callable
from pathlib import Path
from urllib.parse import urlsplit

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
from packaging.version import Version

from app.domain.itemized.hashing import sha256_jcs

StageCallback = Callable[[str, str], Awaitable[None]]
_MAX_LOG = 65536


class InstallationUnavailable(RuntimeError):
    """当前目标不能兑现所选生态的制品合同。"""


def python_package(parameters: dict[str, object]) -> tuple[str, str, str]:
    if parameters.get("registry_type") != "pypi":
        raise InstallationUnavailable("此目标当前仅支持 PyPI wheel 制品安装；npm、OCI、NuGet 尚无已验证发布链")
    if parameters.get("runtime_hint") not in {None, "uv", "uvx"} or parameters.get("runtime_arguments"):
        raise InstallationUnavailable("PyPI 制品安装仅支持 uv／uvx 提示及空 runtimeArguments；不会执行自定义 runtime")
    if parameters.get("expected_sha256") is not None:
        raise InstallationUnavailable("当前 PyPI 安装器使用 uv.lock 的完整依赖校验；尚不支持目录指定的单个 fileSha256")
    name, version = parameters.get("identifier"), parameters.get("version")
    if not isinstance(name, str) or not isinstance(version, str):
        raise TypeError("PyPI 制品必须指定包名与精确版本")
    requirement = Requirement(f"{name}=={version}")
    if requirement.extras or requirement.url or requirement.marker or canonicalize_name(requirement.name) != canonicalize_name(name):
        raise InstallationUnavailable("PyPI 制品当前只支持单个标准 distribution，不支持 extras、URL 或 marker")
    Version(version)
    index = parameters.get("registry_base_url") or "https://pypi.org/simple"
    if not isinstance(index, str):
        raise TypeError("PyPI registryBaseUrl 必须为字符串")
    parsed = urlsplit(index)
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment or (parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"})):
        raise InstallationUnavailable("PyPI index 必须是无凭据的明确 HTTPS 地址；HTTP 仅支持 loopback")
    if shutil.which("uv") is None or not Path(sys.executable).is_file():
        raise InstallationUnavailable("目标缺少已有 uv 或 Python；请在目标安装所需 runtime 后重试，安装器不会全局安装环境")
    return canonicalize_name(name), version, index.rstrip("/")


def artifact_digest(root: Path) -> str:
    """校验固定路径中的已发布文件；日志和安装账本不在此目录。"""
    entries: list[dict[str, object]] = []
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            entry: dict[str, object] = {"path": relative, "symlink": os.readlink(path)}
            if path.is_file():
                with path.open("rb") as stream:
                    entry["sha256"] = hashlib.file_digest(stream, "sha256").hexdigest()
            entries.append(entry)
        elif path.is_file():
            with path.open("rb") as stream:
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
            entries.append({"path": relative, "sha256": digest, "mode": path.stat().st_mode & 0o777})
    return sha256_jcs(entries)


def _tool_environment() -> dict[str, str]:
    # 阻止用户 uv/pip 配置、激活环境与 Python 注入改变本次显式来源及解释器。
    return {key: value for key, value in os.environ.items()
            if not key.upper().startswith(("UV_", "PIP_", "PYTHON")) and key not in {"VIRTUAL_ENV", "CONDA_PREFIX"}}


async def _run(command: list[str], *, directory: Path, progress: StageCallback, stage: str) -> str:
    await progress(stage, "")
    process = await asyncio.create_subprocess_exec(*command, cwd=directory, env=_tool_environment(),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    reader = process.stdout
    assert reader is not None
    chunks: list[bytes] = []
    size = 0

    async def read() -> None:
        nonlocal size
        while chunk := await reader.read(8192):
            # 排空工具输出但只保留固定上限；不把请求输入传给命令或日志。
            if size < _MAX_LOG:
                chunks.append(chunk[:_MAX_LOG - size])
                size += len(chunks[-1])
        await process.wait()

    try:
        async with asyncio.timeout(180):
            await read()
    except BaseException:
        if process.returncode is None:
            process.kill()
        await process.wait()
        raise
    output = b"".join(chunks).decode("utf-8", errors="replace")
    await progress(stage, output)
    if process.returncode != 0:
        raise InstallationUnavailable(f"目标 uv／Python 阶段 {stage} 失败，exit_code={process.returncode}；查看原操作日志")
    return output


async def prepare_python_artifact(root: Path, *, name: str, version: str, index: str,
    entrypoint: str, progress: StageCallback) -> tuple[str, str, dict[str, str]]:
    root.mkdir(parents=True, exist_ok=False)
    # venv 的 shebang 含绝对路径；准备即采用最终 generation 路径，验证后只发布账本。
    project = ("[project]\nname = \"boxteam-mcp-artifact\"\nversion = \"0.0.0\"\n"
        f"requires-python = \"=={sys.version_info.major}.{sys.version_info.minor}.*\"\n"
        f"dependencies = [{json.dumps(name + '==' + version)}]\n")
    (root / "pyproject.toml").write_text(project, encoding="utf-8")
    uv = shutil.which("uv")
    if uv is None:
        raise InstallationUnavailable("目标 uv 已不可用，未尝试动态 uvx 或其它安装方式")
    common = [uv, "--no-config", "--no-cache", "--directory", str(root)]
    options = ["--python", sys.executable, "--no-python-downloads", "--no-managed-python", "--default-index", index, "--no-build"]
    await _run([*common, "lock", *options], directory=root, progress=progress, stage="lock")
    lock = tomllib.loads((root / "uv.lock").read_text(encoding="utf-8"))
    for package in lock["package"]:
        source = package["source"]
        same_index = set(source) == {"registry"} and source["registry"].rstrip("/") == index.rstrip("/")
        if source != {"virtual": "."} and not same_index:
            raise InstallationUnavailable("依赖锁包含所选 PyPI index 之外的来源，未安装或切换到其它来源")
    await _run([*common, "sync", "--locked", "--no-dev", "--no-default-groups", "--no-install-project", "--link-mode", "copy", *options],
        directory=root, progress=progress, stage="install")
    executable_directory = root / ".venv" / ("Scripts" if os.name == "nt" else "bin")
    python = executable_directory / ("python.exe" if os.name == "nt" else "python")
    script = ("import importlib.metadata as m,json,sys; d=m.distribution(sys.argv[1]); "
        "print(json.dumps({'version':d.version,'entrypoints':{e.name:e.value for e in d.entry_points if e.group=='console_scripts'},"
        "'distributions':{v.metadata['Name']:v.version for v in m.distributions()}}))")
    metadata = json.loads(await _run([str(python), "-I", "-B", "-c", script, name], directory=root, progress=progress, stage="verify"))
    if Version(metadata["version"]) != Version(version) or entrypoint not in metadata["entrypoints"]:
        raise InstallationUnavailable("已安装根 distribution 的版本或显式 console entry point 与请求不一致")
    load_script = ("import importlib.metadata as m,sys; "
        "e=next(e for e in m.distribution(sys.argv[1]).entry_points if e.group=='console_scripts' and e.name==sys.argv[2]); "
        "assert callable(e.load()), 'console entry point 必须可调用'")
    await _run([str(python), "-I", "-B", "-c", load_script, name, entrypoint], directory=root, progress=progress, stage="verify")
    command = executable_directory / (entrypoint + ".exe" if os.name == "nt" else entrypoint)
    if not command.is_file():
        raise InstallationUnavailable("生态工具未生成所选 console entry point 可执行文件")
    lock_digest = "sha256:" + hashlib.sha256((root / "uv.lock").read_bytes()).hexdigest()
    return str(command), lock_digest, metadata["distributions"]
