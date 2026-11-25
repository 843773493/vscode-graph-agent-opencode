"""正常 rollout runtime 的格式版本闸门。"""

from __future__ import annotations

from app.domain.itemized.errors import FormatDispatchError
from app.domain.itemized.records import CANONICAL_ITEM_FORMAT_VERSION


def require_current_rollout_format(format_version: int) -> None:
    if type(format_version) is not int or format_version != CANONICAL_ITEM_FORMAT_VERSION:
        raise FormatDispatchError(
            f"unsupported_rollout_format_version: current={format_version!r}, "
            f"expected={CANONICAL_ITEM_FORMAT_VERSION}；原件保留，当前程序不提供旧格式转换"
        )


__all__ = ["require_current_rollout_format"]
