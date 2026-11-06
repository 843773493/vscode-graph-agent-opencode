"""从发行 manifest 取得目标侧专用 Node 宿主，不借用 Launcher 的 Node。"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field


class _Binding(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    node_executable: str = Field(pattern=r"\S")
    component_root: str = Field(pattern=r"\S")


class _Version(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    version: str = Field(min_length=1)
    nodeVersion: str = Field(pattern=r"^\d+\.\d+\.\d+$")


@dataclass(frozen=True, slots=True)
class NodeHostRuntime:
    executable: Path
    entrypoint: Path
    commit: str
    vscode_version: str
    node_version: str


def load_node_host_runtime(manifest_path: Path | str | None = None) -> NodeHostRuntime | None:
    """只解析受信启动入口指定的 manifest；未声明能力不触发发现、下载或启动。"""
    configured = manifest_path if manifest_path is not None else os.environ.get("BOXTEAM_RUNTIME_MANIFEST")
    if configured is None:
        return None
    if not str(configured).strip():
        raise ValueError("Node 宿主 runtime manifest 路径不能为空")
    manifest_path = Path(configured).resolve(strict=True)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict) or type(manifest.get("schema_version")) is not int or manifest["schema_version"] != 1:
        raise ValueError(f"Node 宿主要求 runtime manifest schema_version=1: {manifest_path}")
    raw_binding = manifest.get("extension_host")
    if raw_binding is None:
        return None
    binding = _Binding.model_validate(raw_binding)
    executable = (manifest_path.parent / binding.node_executable).resolve(strict=True)
    component = (manifest_path.parent / binding.component_root).resolve(strict=True)
    if not executable.is_file() or not os.access(executable, os.X_OK):
        raise ValueError(f"Node 宿主 executable 不是可执行文件: {executable}")
    if not component.is_dir():
        raise ValueError(f"Node 宿主 component_root 不是目录: {component}")
    for name in ("extensionHostProcess.mjs", "version.json", "package.json", "package-lock.json"):
        if not (component / name).is_file():
            raise FileNotFoundError(f"Node 宿主组件缺少 {name}: {component}")
    version = _Version.model_validate_json((component / "version.json").read_text(encoding="utf-8"))
    # 构建物入口已校验实际 process.versions.node；沿原启动握手拒绝错误版本，
    # 不为每次连接额外启动版本探针，也不重复 hash 整个依赖目录。
    return NodeHostRuntime(executable, component / "extensionHostProcess.mjs", version.commit,
                           version.version, version.nodeVersion)
