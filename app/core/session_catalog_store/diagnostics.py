"""catalog 原生校验失败的逻辑说明和私有位置共用出口。"""

from __future__ import annotations

from app.core.failures import failure_detail


def catalog_failure(error: BaseException, *, database_path: object, stage: str, reason: str) -> str:
    """位置留诊断，公开说明仅由校验 owner 的已知事实组成。"""
    note = f"Session catalog 校验位置: {database_path}"
    if note not in getattr(error, "__notes__", ()):
        error.add_note(note)
    return failure_detail(error, stage=stage, label=f"{reason}: artifact=session_catalog")
