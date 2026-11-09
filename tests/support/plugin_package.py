"""为本机生成的代码插件测试包声明真实目标。"""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import sys
from functools import cache


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
