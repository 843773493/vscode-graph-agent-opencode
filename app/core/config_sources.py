from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import commentjson

ConfigSourceLayer = Literal["inline", "user", "user_local", "workspace", "sqlite"]


@dataclass(frozen=True, slots=True)
class ConfigSource:
    """描述一个配置层及其在最终配置中的优先级。"""

    path: Path
    layer: ConfigSourceLayer
    precedence: int
    loaded: bool
    source_key: str | None = None
    presence: Literal["present", "absent"] = "present"
    layer_revision: int | None = None
    layer_digest: str | None = None
    source_generation: int | None = None


@dataclass(frozen=True, slots=True)
class StableConfigFile:
    """描述一次前后字节一致的 JSONC 文件读取。"""

    path: Path
    presence: Literal["present", "absent"]
    raw_bytes: bytes | None
    digest: str | None
    stat_signature: tuple[int, int, int, int] | None = None


def _config_file_stat_signature(stat_result: os.stat_result) -> tuple[int, int, int, int]:
    """提取用于 TOCTOU 检查的最小文件身份签名。"""

    return (
        int(stat_result.st_dev),
        int(stat_result.st_ino),
        int(stat_result.st_size),
        int(stat_result.st_mtime_ns),
    )


def read_stable_config_file(path: Path, *, attempts: int = 3) -> StableConfigFile:
    """读取稳定文件快照；文件在读取期间变化时直接报错。"""

    resolved_path = path.expanduser().resolve()
    if attempts < 1:
        raise ValueError("稳定配置文件读取次数必须大于 0")

    for _ in range(attempts):
        try:
            before = resolved_path.stat()
        except FileNotFoundError:
            return StableConfigFile(
                path=resolved_path,
                presence="absent",
                raw_bytes=None,
                digest=None,
                stat_signature=None,
            )
        raw_bytes = resolved_path.read_bytes()
        try:
            after = resolved_path.stat()
        except FileNotFoundError:
            continue
        if (
            before.st_dev == after.st_dev
            and before.st_ino == after.st_ino
            and before.st_size == after.st_size
            and before.st_mtime_ns == after.st_mtime_ns
        ):
            return StableConfigFile(
                path=resolved_path,
                presence="present",
                raw_bytes=raw_bytes,
                digest=hashlib.sha256(raw_bytes).hexdigest(),
                stat_signature=_config_file_stat_signature(after),
            )

    raise RuntimeError(f"配置文件在读取期间持续变化，无法建立稳定快照: {resolved_path}")


def verify_stable_config_file(snapshot: StableConfigFile) -> None:
    """在持久化 source layer 前确认文件仍是已解析的那一版。"""

    current = read_stable_config_file(snapshot.path)
    if (
        current.presence != snapshot.presence
        or current.digest != snapshot.digest
        or current.stat_signature != snapshot.stat_signature
    ):
        raise RuntimeError(
            "配置文件在解析后发生变化，拒绝提交 source layer: "
            f"{snapshot.path}"
        )


def parse_stable_config_file(
    snapshot: StableConfigFile,
) -> dict[str, object] | None:
    """解析已经建立的稳定 JSONC 快照；absent 文件返回 None。"""

    if snapshot.presence == "absent":
        return None
    if snapshot.raw_bytes is None:
        raise RuntimeError(f"present 配置快照缺少文件内容: {snapshot.path}")
    parsed = commentjson.loads(snapshot.raw_bytes.decode("utf-8"))
    if not isinstance(parsed, dict):
        raise TypeError(f"配置文件根节点必须是对象: {snapshot.path}")
    return parsed


def config_revision(config: dict[str, object]) -> str:
    """根据最终配置内容生成稳定修订号。"""

    canonical_json = json.dumps(
        config,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()
