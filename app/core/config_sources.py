from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

ConfigSourceLayer = Literal["inline", "user", "user_local", "workspace", "sqlite"]


@dataclass(frozen=True, slots=True)
class ConfigSource:
    """描述一个配置层及其在最终配置中的优先级。"""

    path: Path
    layer: ConfigSourceLayer
    precedence: int
    loaded: bool


def config_revision(config: dict[str, object]) -> str:
    """根据最终配置内容生成稳定修订号。"""

    canonical_json = json.dumps(
        config,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()
