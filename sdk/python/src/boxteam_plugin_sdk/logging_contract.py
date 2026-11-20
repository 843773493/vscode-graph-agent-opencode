"""SDK 包内只读日志格式合同。"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path


@dataclass(frozen=True, slots=True)
class RuntimeLogContract:
    root_parts: tuple[str, ...]
    paths: Mapping[str, tuple[str, ...]]
    max_bytes: int
    archives: int
    inactive_max_bytes: int
    levels: Mapping[str, int]
    default_level: str
    default_pretty: bool
    pretty_format: str
    compact_format: str
    context_fields: tuple[str, ...]

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


def load_runtime_log_contract() -> RuntimeLogContract:
    content = files("boxteam_plugin_sdk").joinpath("resources/runtime-contract.json").read_text(encoding="utf-8")
    payload = json.loads(content)
    if payload["version"] != 1:
        raise ValueError("不支持的产品日志合同版本")
    return RuntimeLogContract(
        root_parts=tuple(payload["root"]),
        paths={role: tuple(parts) for role, parts in payload["paths"].items()},
        max_bytes=payload["rotation"]["max_bytes"],
        archives=payload["rotation"]["archives"],
        inactive_max_bytes=payload["retention"]["inactive"]["max_bytes"],
        levels=payload["record"]["levels"],
        default_level=payload["record"]["defaults"]["level"],
        default_pretty=payload["record"]["defaults"]["pretty"],
        pretty_format=payload["record"]["pretty"],
        compact_format=payload["record"]["compact"],
        context_fields=tuple(payload["record"]["context_fields"]),
    )
