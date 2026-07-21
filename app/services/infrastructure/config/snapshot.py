from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal


ConfigReloadFailureReason = Literal[
    "invalid_config",
    "restart_required",
    "apply_failed",
]


@dataclass(frozen=True, slots=True)
class ConfigSnapshot:
    revision: str
    canonical_json: str
    source_paths: tuple[Path, ...]
    loaded_at: datetime

    def to_dict(self) -> dict[str, Any]:
        value = json.loads(self.canonical_json)
        if not isinstance(value, dict):
            raise TypeError("配置快照根节点必须是对象")
        return value


@dataclass(frozen=True, slots=True)
class ConfigReloadStatus:
    healthy: bool
    revision: str
    last_success_at: datetime
    last_attempt_at: datetime
    last_error: str | None
    restart_required: bool = False
    reason: ConfigReloadFailureReason | None = None
    changed_sections: tuple[str, ...] = ()


class ConfigRestartRequiredError(RuntimeError):
    def __init__(self, message: str, *, changed_sections: tuple[str, ...]) -> None:
        super().__init__(message)
        if not changed_sections:
            raise ValueError("需要重启时必须提供至少一个配置 section")
        self.changed_sections = tuple(dict.fromkeys(changed_sections))


def build_config_snapshot(
    config: dict[str, Any],
    *,
    source_paths: tuple[Path, ...],
) -> ConfigSnapshot:
    canonical_json = json.dumps(
        config,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return ConfigSnapshot(
        revision=hashlib.sha256(canonical_json.encode("utf-8")).hexdigest(),
        canonical_json=canonical_json,
        source_paths=source_paths,
        loaded_at=datetime.now(timezone.utc),
    )
