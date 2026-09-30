"""ConfigService 拆分后的共享模块级符号：logger、候选应用器别名、来源层权威表与 inline VRN 判定（唯一定义点）。"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from pathlib import Path

from app.core.config_sources import (
    ConfigSourceLayer,
)
from app.services.infrastructure.config import (
    ConfigSnapshot,
)
from app.services.infrastructure.config.source_vrn import inline_config_source_vrn
from configs.installer import resolve_config_resource_source

logger = logging.getLogger(__name__)

ConfigCandidateApplier = Callable[[ConfigSnapshot, ConfigSnapshot], Awaitable[None]]

# 来源权威表：每个 source_key 的逻辑来源层与 precedence 只在这里登记一次。
# `_config_source`（从源 JSONC 构建）、`_runtime_override_source`、
# `_persisted_source_details`（从 active snapshot 基线恢复）都只查这一张表，MUST NOT
# 任何一条读路径再自行推导——那正是双轨，会让同一 source_key 报出不同 layer。
# inline 用固定权威键 `_INLINE_SOURCE_KEY`：发行包内文件，其 `source_key` 对外为 None，
# 绝不参与分层兜底（否则会被错标成 `sqlite` 且 precedence 由 0 翻成 1）。
_INLINE_SOURCE_KEY = "inline"
_SOURCE_LAYER_AUTHORITY: dict[str, tuple[ConfigSourceLayer, int]] = {
    _INLINE_SOURCE_KEY: ("inline", 0),
    "workspace_mutable_override": ("user", 1),
    "workspace_local_mutable_override": ("user_local", 2),
    "workspace_root_mutable_override": ("workspace", 3),
    "workspace_runtime_override": ("sqlite", 4),
}


def release_inline_config_vrn_for_file(
    effective_path: Path | None,
    *,
    release_config_name: str,
) -> str | None:
    """生效文件若与发行包同名配置内容一致，返回其 inline VRN，否则 None。

    安装链路把发行包内配置与 schema **原字节拷贝**到用户配置目录（`configs/installer.py`
    的 `atomic_write(target, source.read_bytes())`），故「真属发行包 inline 层」的正确
    判据是**与发行包资源内容一致**，而非路径相等——路径相等会把正常安装的拷贝误判为非
    inline。判据失败（路径缺失、不可读、内容不同）一律返回 None，MUST NOT 抛错：调用方
    以空字符串对外表达不可寻址，故含点号或非法字符的用户自定义文件名永不进入 VRN 构造。
    VRN 尾段取发行包资源的逻辑名（`release_config_name` 去扩展名），MUST NOT 取生效文件 stem。
    """
    if effective_path is None:
        return None
    try:
        release_path = resolve_config_resource_source(release_config_name)
    except FileNotFoundError:
        return None
    try:
        if effective_path.read_bytes() != release_path.read_bytes():
            return None
    except OSError:
        return None
    return inline_config_source_vrn(logical_name=Path(release_config_name).stem)


__all__ = ["_SOURCE_LAYER_AUTHORITY", "ConfigCandidateApplier", "logger", "release_inline_config_vrn_for_file"]
