"""发行包运行时身份：从 runtime manifest 推导稳定的 distribution_id。

``distribution_id`` 是 URI ``inline`` scope 的 ``scope_id``，其唯一 owner 是
``add-unified-virtual-resource-addressing`` 的 requirement「inline scope 的
scope_id 由 manifest 的 distribution 与 version 定稿推导」：只依赖发行包
runtime manifest 的 ``distribution`` 与 ``version`` 两个字段，因此同一发行包
在任意机器、任意安装路径下都算出同一个值（跨 gateway 寻址的前提）。

manifest 的定位方式与 Launcher 一致：显式 ``BOXTEAM_RUNTIME_MANIFEST`` 环境变量
指向 manifest 文件本身（Launcher 与 Gateway 在启动工作区后端时注入）。
"""

from __future__ import annotations

import json
import logging
import os
import re
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

logger = logging.getLogger(__name__)
MANIFEST_ENV = "BOXTEAM_RUNTIME_MANIFEST"

# URI 动段的闭合 charset 是 ``[A-Za-z0-9_-]``（grammar 的 _NAME_CHARSET 单点
# 定义）；点号不在内，故 version 必须编码。distribution 只接受 Launcher schema 的
# 四值闭枚举；专属单测核对 packages/launcher/runtime-manifest.schema.json，防止漂移。
_DISTRIBUTIONS = ("source-development", "source-installed", "npm", "standalone")
# version 只接受 semver 允许的形态：pre-release/build 标识的字符集是
# ``[0-9A-Za-z-]``，故下划线 ``_`` 本就非法，在此显式排除。排除 ``_`` 是编码成为
# 单射的前提（见 encode_version），含 ``_`` 的 manifest 一律 fail-closed 拒绝。
_VERSION_PATTERN = re.compile(r"^[A-Za-z0-9.-]+$")


def encode_version(version: str) -> str:
    """把 version 编码进 URI charset：``.`` → ``_``。

    因 version 字符集已排除 ``_``（见 _VERSION_PATTERN），``_`` 只可能来自原点号，
    故本映射在合法输入上是单射：不同的 version 必得不同编码，且可由编码唯一还原
    （单射性由 tests/unit/core/identity/test_distribution_identity.py 的穷举断言证明）。

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
    if distribution not in _DISTRIBUTIONS:
        raise ValueError(_manifest_failure(resolved_path, "validate",
            f"runtime manifest.distribution 未登记值，只允许 {', '.join(_DISTRIBUTIONS)}",
            field="distribution", value=distribution))
    if _VERSION_PATTERN.fullmatch(version) is None:
        raise ValueError(_manifest_failure(resolved_path, "validate",
            "runtime manifest.version 只允许 [A-Za-z0-9.-]（semver 形态，不含下划线）",
            field="version", value=version))
    return f"{distribution}-{encode_version(version)}"


def _manifest_failure(path: Path, stage: str, reason: str, *, field: str | None = None,
                      value: object = None, error: BaseException | None = None) -> str:
    """发行身份尚未生成时，用逻辑 artifact 定位；原值只留原生诊断。"""
    detail = f"{reason}: artifact=runtime_manifest, stage={stage}, field={field}"
    logger.error("%s path=%s value=%r", detail, path, value,
                 exc_info=None if error is None else (type(error), error, error.__traceback__))
    return detail


@contextmanager
def _manifest_access(path: Path, stage: str) -> Iterator[None]:
    try:
        yield
    except (OSError, RuntimeError) as error:
        detail = _manifest_failure(path, stage,
            f"runtime manifest 访问失败: error={type(error).__name__}, "
            f"errno={getattr(error, 'errno', None)}, winerror={getattr(error, 'winerror', None)}", error=error)
        if isinstance(error, OSError):
            raise type(error)(error.errno, detail, None, getattr(error, "winerror", None)) from error
        raise RuntimeError(detail) from error


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
    with _manifest_access(candidate, "resolve"):
        resolved = candidate.expanduser().resolve()
        present = resolved.is_file()
    if not present:
        raise FileNotFoundError(_manifest_failure(resolved, "resolve", "runtime manifest 不存在"))
    return resolved


def _load_manifest(path: Path) -> dict[str, object]:
    try:
        with _manifest_access(path, "read"):
            content = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as error:
        detail = _manifest_failure(path, "decode", "runtime manifest 不是合法 UTF-8", error=error)
        raise UnicodeDecodeError(error.encoding, error.object, error.start, error.end, detail) from error
    try:
        payload = json.loads(content)
    except json.JSONDecodeError as error:
        raise ValueError(_manifest_failure(path, "decode",
            f"runtime manifest 不是合法 JSON: line={error.lineno}, column={error.colno}", error=error)) from error
    if not isinstance(payload, dict):
        raise ValueError(_manifest_failure(path, "validate", "runtime manifest 必须是 JSON 对象", value=payload))  # noqa: TRY004 - 保留既有拒绝类型。
    return payload


def _require_field(payload: dict[str, object], field: str, path: Path) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(_manifest_failure(path, "validate", f"runtime manifest.{field} 必须是非空字符串",
                                           field=field, value=value))
    return value
