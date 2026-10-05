"""Backend 进程启动时固定的已挂载工作区注册表。"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

from app.core.workspace_identity import bind_workspace_id, validate_workspace_id


@dataclass(frozen=True, slots=True)
class MountedWorkspace:
    workspace_id: str
    workspace_root: Path


class MountedWorkspaceRegistry:
    """提供按 backend workspace UUID 精确查找的只读 mount 集合。"""

    ENVIRONMENT_VARIABLE: ClassVar[str] = "BOXTEAM_MOUNTED_WORKSPACES"

    def __init__(self, mounts: list[MountedWorkspace]) -> None:
        if not isinstance(mounts, list) or not mounts:
            raise ValueError("已挂载工作区集合必须是非空数组")

        by_id: dict[str, MountedWorkspace] = {}
        roots: dict[Path, str] = {}
        normalized: list[MountedWorkspace] = []
        for mount in mounts:
            if not isinstance(mount, MountedWorkspace):
                raise TypeError(f"mount 必须是 MountedWorkspace: {mount!r}")
            workspace_id = validate_workspace_id(mount.workspace_id)
            workspace_root = mount.workspace_root.expanduser().resolve(strict=True)
            if not workspace_root.is_dir():
                raise NotADirectoryError(f"挂载工作区根不是目录: {workspace_root}")
            if workspace_id in by_id:
                raise ValueError(f"挂载集合包含重复 workspace_id: {workspace_id}")
            previous_id = roots.get(workspace_root)
            if previous_id is not None:
                raise ValueError(
                    "挂载集合包含重复工作区根: "
                    f"workspace_id={workspace_id}, previous_workspace_id={previous_id}, "
                    f"root={workspace_root}"
                )
            bind_workspace_id(workspace_root, workspace_id)
            normalized_mount = MountedWorkspace(workspace_id, workspace_root)
            by_id[workspace_id] = normalized_mount
            roots[workspace_root] = workspace_id
            normalized.append(normalized_mount)

        self._by_id = by_id
        self._mounts = tuple(normalized)

    @classmethod
    def from_environment(cls) -> MountedWorkspaceRegistry:
        raw_value = os.environ.get(cls.ENVIRONMENT_VARIABLE)
        if raw_value is None or not raw_value.strip():
            raise RuntimeError(
                f"Backend 启动必须显式提供 {cls.ENVIRONMENT_VARIABLE}"
            )
        try:
            payload = json.loads(raw_value)
        except json.JSONDecodeError as error:
            raise ValueError(
                f"{cls.ENVIRONMENT_VARIABLE} 必须是 JSON 数组: {error}"
            ) from error
        return cls.from_payload(payload)

    @classmethod
    def from_payload(cls, payload: object) -> MountedWorkspaceRegistry:
        if not isinstance(payload, list) or not payload:
            raise ValueError(
                f"{cls.ENVIRONMENT_VARIABLE} 必须是非空 JSON 数组"
            )

        mounts: list[MountedWorkspace] = []
        for index, item in enumerate(payload):
            if not isinstance(item, dict) or set(item) != {
                "workspace_id",
                "workspace_root",
            }:
                raise ValueError(
                    f"{cls.ENVIRONMENT_VARIABLE}[{index}] 必须只含 workspace_id 和 "
                    "workspace_root"
                )
            workspace_id = item["workspace_id"]
            workspace_root = item["workspace_root"]
            if not isinstance(workspace_id, str) or not isinstance(
                workspace_root, str
            ):
                raise TypeError(
                    f"{cls.ENVIRONMENT_VARIABLE}[{index}] 的 workspace_id 和 "
                    "workspace_root 必须是字符串"
                )
            if not Path(workspace_root).is_absolute():
                raise ValueError(
                    f"{cls.ENVIRONMENT_VARIABLE}[{index}].workspace_root 必须是绝对路径"
                )
            mounts.append(MountedWorkspace(workspace_id, Path(workspace_root)))
        return cls(mounts)

    @property
    def workspace_ids(self) -> tuple[str, ...]:
        return tuple(mount.workspace_id for mount in self._mounts)

    @property
    def mounts(self) -> tuple[MountedWorkspace, ...]:
        return self._mounts

    def resolve(self, workspace_id: str) -> MountedWorkspace:
        canonical_id = validate_workspace_id(workspace_id)
        try:
            return self._by_id[canonical_id]
        except KeyError as error:
            raise KeyError(f"未挂载的工作区 UUID: {canonical_id}") from error


__all__ = ["MountedWorkspace", "MountedWorkspaceRegistry"]
