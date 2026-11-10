"""为本机生成的代码插件测试包声明真实目标。"""

from __future__ import annotations

import asyncio
import json
import os
import platform
import shutil
import subprocess
import sys
import time
from collections.abc import Awaitable, Callable
from functools import cache
from typing import Literal
from uuid import uuid4

import httpx

from app.schemas.gateway.plugin_installation import PluginInstallOperationDTO

_INSTALL_ENDPOINT = "/api/gateway/service-plugins/install"
_INSTALL_OPERATION_ENDPOINT = "/api/gateway/service-plugins/install-operation"
_INSTALL_WAIT_SECONDS = 60


@cache
def native_plugin_target(*runtimes: str) -> dict[str, object]:
    """只描述测试包目标；不准备依赖或绕过产品安装校验。"""
    target_os = {"linux": "linux", "win32": "windows", "darwin": "macos"}[sys.platform]
    architecture = {"x86_64": "x64", "amd64": "x64", "aarch64": "arm64", "arm64": "arm64"}[platform.machine().lower()]
    if target_os == "linux":
        libc, version = platform.libc_ver()
        if libc != "glibc":
            raise RuntimeError(f"插件测试包需要明确的 glibc 目标，实际为 {libc}")
        baseline = f"glibc-{version}"
    elif target_os == "windows":
        if sys.platform != "win32":
            raise RuntimeError("Windows 插件目标只能在 Windows 主机上生成")
        version = sys.getwindowsversion()
        baseline = f"windows-{version.major}.{version.minor}.{version.build}"
    else:
        baseline = f"macos-{platform.mac_ver()[0]}"
    target: dict[str, object] = {
        "os": target_os, "architecture": architecture, "system_baseline": baseline,
    }
    if "python" in runtimes:
        target["python"] = {"version": platform.python_version(),
                            "abi": f"cp{sys.version_info.major}{sys.version_info.minor}"}
    if "node" in runtimes:
        executable = os.environ.get("BOXTEAM_NODE_BIN") or shutil.which("node")
        if not executable:
            raise RuntimeError("Node 插件测试需要目标 Node 解释器")
        result = subprocess.run([executable, "--print",
            "JSON.stringify({version:process.versions.node,abi:process.versions.modules})"],
            capture_output=True, text=True, check=True, timeout=5)
        target["node"] = json.loads(result.stdout)
    return target


def new_plugin_install_operation_id() -> str:
    return f"test-install-{uuid4().hex}"


def _install_payload(
    gateway_id: str,
    path: str,
    operation_id: str,
    expected_digest: str | None,
    development_directory: bool,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "gateway_id": gateway_id,
        "operation_id": operation_id,
        "path": path,
        "development_directory": development_directory,
    }
    if expected_digest is not None:
        payload["expected_digest"] = expected_digest
    return payload


def _install_operation(response: httpx.Response, operation_id: str) -> PluginInstallOperationDTO:
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), dict):
        raise TypeError(f"插件安装响应缺少 operation data: {response.text}")
    operation = PluginInstallOperationDTO.model_validate(payload["data"])
    if operation.operation_id != operation_id:
        raise AssertionError(
            f"插件安装响应 operation_id 不匹配: expected={operation_id}, actual={operation.operation_id}"
        )
    return operation


def _require_install_status(
    operation: PluginInstallOperationDTO,
    expected_status: Literal["succeeded", "failed"],
) -> PluginInstallOperationDTO:
    if operation.status in {"unknown", "cancelled"} or operation.status != expected_status:
        detail = f"; error={operation.error}" if operation.error else ""
        raise AssertionError(
            f"插件安装操作 {operation.operation_id} 进入 {operation.status}，expected={expected_status}{detail}"
        )
    return operation


def _install_plugin(
    post: Callable[[str, dict[str, object]], httpx.Response],
    *,
    gateway_id: str,
    path: str,
    expected_digest: str | None = None,
    development_directory: bool = False,
    expected_status: Literal["succeeded", "failed"],
) -> PluginInstallOperationDTO:
    """只提交一次安装，并按相同 operation_id 轮询到终态。"""
    operation_id = new_plugin_install_operation_id()
    operation = _install_operation(
        post(
            _INSTALL_ENDPOINT,
            _install_payload(gateway_id, path, operation_id, expected_digest, development_directory),
        ),
        operation_id,
    )
    deadline = time.monotonic() + _INSTALL_WAIT_SECONDS
    while operation.status in {"pending", "running"}:
        if time.monotonic() >= deadline:
            raise AssertionError(f"插件安装操作等待超时: {operation_id}, status={operation.status}")
        time.sleep(0.05)
        operation = _install_operation(
            post(
                _INSTALL_OPERATION_ENDPOINT,
                {"gateway_id": gateway_id, "operation_id": operation_id},
            ),
            operation_id,
        )
    return _require_install_status(operation, expected_status)


def install_plugin(
    post: Callable[[str, dict[str, object]], httpx.Response],
    *,
    gateway_id: str,
    path: str,
    expected_digest: str | None = None,
    development_directory: bool = False,
) -> PluginInstallOperationDTO:
    """只提交一次安装，并按相同 operation_id 轮询到 succeeded。"""
    return _install_plugin(
        post,
        gateway_id=gateway_id,
        path=path,
        expected_digest=expected_digest,
        development_directory=development_directory,
        expected_status="succeeded",
    )


def expect_plugin_install_failure(
    post: Callable[[str, dict[str, object]], httpx.Response],
    *,
    gateway_id: str,
    path: str,
    expected_digest: str | None = None,
    development_directory: bool = False,
) -> PluginInstallOperationDTO:
    """等待唯一安装操作失败；取消或未知结果仍然是测试失败。"""
    return _install_plugin(
        post,
        gateway_id=gateway_id,
        path=path,
        expected_digest=expected_digest,
        development_directory=development_directory,
        expected_status="failed",
    )


async def install_plugin_async(
    post: Callable[[str, dict[str, object]], Awaitable[httpx.Response]],
    *,
    gateway_id: str,
    path: str,
    expected_digest: str | None = None,
    development_directory: bool = False,
) -> PluginInstallOperationDTO:
    """异步版本的单次安装与 operation_id 状态轮询。"""
    operation_id = new_plugin_install_operation_id()
    operation = _install_operation(
        await post(
            _INSTALL_ENDPOINT,
            _install_payload(gateway_id, path, operation_id, expected_digest, development_directory),
        ),
        operation_id,
    )
    deadline = time.monotonic() + _INSTALL_WAIT_SECONDS
    while operation.status in {"pending", "running"}:
        if time.monotonic() >= deadline:
            raise AssertionError(f"插件安装操作等待超时: {operation_id}, status={operation.status}")
        await asyncio.sleep(0.05)
        operation = _install_operation(
            await post(
                _INSTALL_OPERATION_ENDPOINT,
                {"gateway_id": gateway_id, "operation_id": operation_id},
            ),
            operation_id,
        )
    return _require_install_status(operation, "succeeded")
