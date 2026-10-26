"""产品日志布局与轮转合同；文件句柄仍由启动 owner 管理。"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from app.core.env import get_project_root
from app.core.path_utils import get_boxteam_home


@dataclass(frozen=True, slots=True)
class RuntimeLogContract:
    root_parts: tuple[str, ...]
    paths: Mapping[str, tuple[str, ...]]
    max_bytes: int
    archives: int

    def root_for(self, boxteam_home: Path) -> Path:
        return boxteam_home.resolve().joinpath(*self.root_parts)

    def path_for(self, root: Path, role: str, instance: str | int | None = None) -> Path:
        if role not in self.paths:
            raise ValueError("进程日志角色非法")
        parts = self.paths[role]
        needs_instance = "{instance}" in parts[-1]
        identity = "" if instance is None else str(instance)
        if needs_instance:
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", identity):
                raise ValueError("进程日志实例标识非法")
        elif instance is not None:
            raise ValueError("进程日志角色不接受实例参数")
        return root.joinpath(*(part.replace("{instance}", identity) for part in parts))


def load_runtime_log_contract(project_root: Path | None = None) -> RuntimeLogContract:
    resource = get_project_root(project_root) / "resources/logging/runtime-contract.json"
    payload = json.loads(resource.read_text(encoding="utf-8"))
    if payload["version"] != 1:
        raise ValueError("不支持的产品日志合同版本")
    return RuntimeLogContract(
        root_parts=tuple(payload["root"]),
        paths={role: tuple(parts) for role, parts in payload["paths"].items()},
        max_bytes=payload["rotation"]["max_bytes"],
        archives=payload["rotation"]["archives"],
    )


def get_runtime_log_root() -> Path:
    return load_runtime_log_contract().root_for(get_boxteam_home())
