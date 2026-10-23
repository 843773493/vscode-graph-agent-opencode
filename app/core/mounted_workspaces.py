"""Backend 进程启动时固定的已挂载工作区注册表。"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import ClassVar
from urllib.parse import urlparse

from app.core.workspace_identity import bind_workspace_id, validate_workspace_id


@dataclass(frozen=True, slots=True)
class MountedWorkspace:
    workspace_id: str
    workspace_root: Path
    terminal_backend_url: str | None = None
    browser_backend_url: str | None = None
    config_candidate_ref: str | None = None
    config_generation: str | None = None
    config_fencing_token: str | None = None

    def __post_init__(self) -> None:
        values = (self.config_candidate_ref, self.config_generation, self.config_fencing_token)
        if any(value is not None for value in values) and not all(
            isinstance(value, str) and value.strip() for value in values
        ):
            raise ValueError("mount 配置启动必须同时提供 candidate_ref、generation 和 fencing token")
        for url in (self.terminal_backend_url, self.browser_backend_url):
            if url is not None and (
                not isinstance(url, str) or urlparse(url).scheme not in {"http", "https"}
                or not urlparse(url).netloc
            ):
                raise ValueError(f"mount 辅助服务 URL 非法: {url!r}")

    def to_payload(self) -> dict[str, object]:
        return {**asdict(self), "workspace_root": str(self.workspace_root)}

    def manifest_payload(self) -> dict[str, object]:
        # candidate 由每工作区配置 owner 独占；组身份只验证 UUID、根与辅助服务绑定。
        return {
            "workspace_id": self.workspace_id, "workspace_root": str(self.workspace_root),
            "terminal_backend_url": self.terminal_backend_url,
            "browser_backend_url": self.browser_backend_url,
        }


def mounted_workspace_manifest_digest(mounts: tuple[MountedWorkspace, ...]) -> str:
    payload = sorted((mount.manifest_payload() for mount in mounts), key=lambda item: str(item["workspace_id"]))
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


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
            normalized_mount = MountedWorkspace(
                workspace_id, workspace_root, mount.terminal_backend_url, mount.browser_backend_url,
                mount.config_candidate_ref, mount.config_generation, mount.config_fencing_token,
            )
            by_id[workspace_id] = normalized_mount
            roots[workspace_root] = workspace_id
            normalized.append(normalized_mount)

        self._by_id = by_id
        self._mounts = tuple(normalized)
        self._manifest_digest = mounted_workspace_manifest_digest(self._mounts)

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
                "workspace_root", "terminal_backend_url", "browser_backend_url",
                "config_candidate_ref", "config_generation", "config_fencing_token",
            }:
                raise ValueError(
                    f"{cls.ENVIRONMENT_VARIABLE}[{index}] 必须是完整的 mount 启动合同"
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
            mounts.append(MountedWorkspace(
                workspace_id, Path(workspace_root),
                item["terminal_backend_url"], item["browser_backend_url"],
                item["config_candidate_ref"], item["config_generation"], item["config_fencing_token"],
            ))
        return cls(mounts)

    @property
    def manifest_digest(self) -> str:
        return self._manifest_digest

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
