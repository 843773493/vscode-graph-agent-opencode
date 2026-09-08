"""一次性 legacy import 与正常 v2 runtime 的 format 边界。"""

from __future__ import annotations

from app.domain.itemized.errors import FormatDispatchError


def require_v2_runtime(format_version: int, *, expected: int = 2) -> None:
    if type(expected) is not int or expected != 2:
        raise FormatDispatchError("正常 runtime 的 expected format 必须固定为 2")
    if type(format_version) is not int or format_version not in {1, 2}:
        raise FormatDispatchError(
            f"unsupported_rollout_format_version: {format_version!r}"
        )
    if format_version == 1:
        raise FormatDispatchError(
            "v1_migration_required: 正常 runtime/history/provider/checkpoint "
            "只接受 v2 rollout；请显式运行 legacy_import_v1_to_v2"
        )


__all__ = ["require_v2_runtime"]
