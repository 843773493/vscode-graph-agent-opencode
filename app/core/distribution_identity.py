"""发行包运行时身份：从 runtime manifest 推导稳定的 distribution_id。

``distribution_id`` 是 VRN ``inline`` scope 的 ``scope_id``，其唯一 owner 是
``add-unified-virtual-resource-addressing`` 的 requirement「inline scope 的
scope_id 由 manifest 的 distribution 与 version 定稿推导」：只依赖发行包
runtime manifest 的 ``distribution`` 与 ``version`` 两个字段，因此同一发行包
在任意机器、任意安装路径下都算出同一个值（跨 gateway 寻址的前提）。

manifest 的定位方式与 Launcher 一致：显式 ``BOXTEAM_RUNTIME_MANIFEST`` 环境变量
指向 manifest 文件本身（Launcher 与 Gateway 在启动工作区后端时注入）。
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

MANIFEST_ENV = "BOXTEAM_RUNTIME_MANIFEST"

# VRN 动段的闭合 charset 是 ``[A-Za-z0-9_-]``（grammar 的 _NAME_CHARSET 单点
# 定义）；点号不在内，故 version 必须编码。distribution 本身落在该 charset 内。
_DISTRIBUTION_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")
# version 只接受 semver 允许的形态：pre-release/build 标识的字符集是
# ``[0-9A-Za-z-]``，故下划线 ``_`` 本就非法，在此显式排除。排除 ``_`` 是编码成为
# 单射的前提（见 encode_version），含 ``_`` 的 manifest 一律 fail-closed 拒绝。
_VERSION_PATTERN = re.compile(r"^[A-Za-z0-9.-]+$")


def encode_version(version: str) -> str:
    """把 version 编码进 VRN charset：``.`` → ``_``。

    因 version 字符集已排除 ``_``（见 _VERSION_PATTERN），``_`` 只可能来自原点号，
    故本映射在合法输入上是单射：不同的 version 必得不同编码，且可由编码唯一还原
    （单射性由 tests/unit/core/test_distribution_identity.py 的穷举断言证明）。

    直接拼接带点号的 version（如 ``source-development-0.0.2``）会被 grammar 以
    invalid_character 结构化拒绝，故必须走本编码。
    """
    return version.replace(".", "_")


def load_distribution_id(manifest_path: Path | str | None = None) -> str:
    """推导当前发行包的 distribution_id（inline scope 的 scope_id）。

    manifest 缺失、字段缺失或字段非法一律 fail-closed 抛出，绝不回退到
    ``local`` 一类虚假默认值。
    """
    resolved_path = _resolve_manifest_path(manifest_path)
    payload = _load_manifest(resolved_path)
    distribution = _require_field(payload, "distribution", resolved_path)
    version = _require_field(payload, "version", resolved_path)
    if _DISTRIBUTION_PATTERN.fullmatch(distribution) is None:
        raise ValueError(
            "runtime manifest.distribution 含 VRN charset 之外的字符: "
            f"{distribution!r} ({resolved_path})"
        )
    if _VERSION_PATTERN.fullmatch(version) is None:
        raise ValueError(
            "runtime manifest.version 只允许 [A-Za-z0-9.-]（semver 形态，不含下划线），实际为: "
            f"{version!r} ({resolved_path})"
        )
    return f"{distribution}-{encode_version(version)}"


def _resolve_manifest_path(manifest_path: Path | str | None) -> Path:
    if manifest_path is not None:
        candidate = Path(manifest_path)
    else:
        configured = os.environ.get(MANIFEST_ENV)
        if configured is None or not configured.strip():
            raise RuntimeError(
                f"未设置 {MANIFEST_ENV}，无法推导 distribution_id；"
                "请经 Launcher 启动工作区后端，或显式传入 runtime manifest 路径"
            )
        candidate = Path(configured.strip())
    resolved = candidate.expanduser().resolve()
    if not resolved.is_file():
        raise FileNotFoundError(f"runtime manifest 不存在: {resolved}")
    return resolved


def _load_manifest(path: Path) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ValueError(f"runtime manifest 不是合法 JSON: {path}: {error}") from error
    if not isinstance(payload, dict):
        raise ValueError(f"runtime manifest 必须是 JSON 对象: {path}")
    return payload


def _require_field(payload: dict[str, object], field: str, path: Path) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"runtime manifest.{field} 必须是非空字符串: {path}")
    return value

