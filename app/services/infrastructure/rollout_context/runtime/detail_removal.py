"""Context detail 删除与 owner 已批准候选的 retention GC。"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime

from app.domain.itemized.identity.detail_ref import DetailRef
from app.services.infrastructure.rollout_context.runtime.detail_files import DetailFiles
from app.services.infrastructure.rollout_context.runtime.detail_manifest import (
    DetailRecord,
    expiry_time,
)
from app.services.infrastructure.rollout_context.runtime.detail_payload import (
    parse_detail_payload,
)


class DetailRemoval:
    """只删除精确 typed locator；引用资格与先写 tombstone 均由调用 owner 负责。"""

    def __init__(self, files: DetailFiles) -> None:
        self._files = files

    def remove(
        self, *, session_id: str, thread_id: str, record: DetailRecord
    ) -> None:
        record.detail_ref.require_owner(session_id, thread_id)
        self._remove_ref(record.detail_ref)

    def _remove_ref(
        self, ref: DetailRef, *, storage_owner: tuple[str, str] | None = None,
    ) -> bool:
        # 两个位置全部 preflight 后再删除；保留普通 manifest 到最后，便于重试。
        paths = self._files.remove_paths(ref, storage_owner=storage_owner)
        for path in reversed(paths):
            path.unlink()
        return bool(paths)

    def gc(
        self,
        *,
        session_id: str,
        thread_id: str,
        expired_before: datetime,
        allowed_refs: Iterable[DetailRef],
    ) -> tuple[DetailRef, ...]:
        """仅处理已提交 tombstone 的候选，不扫描目录推导身份或 GC 资格。"""
        if not isinstance(session_id, str) or not session_id or "\x00" in session_id:
            raise ValueError("session_id 必须是非空且不含 NUL 的字符串")
        if (
            not isinstance(expired_before, datetime)
            or expired_before.utcoffset() is None
        ):
            raise ValueError("expired_before 必须是带时区的 datetime")
        refs = tuple(allowed_refs)
        storage_owner = (session_id, thread_id)
        # 先校验整批身份，不能删除前几项后才发现跨 session 输入。
        for ref in refs:
            if not isinstance(ref, DetailRef):
                raise TypeError("GC allowed_refs 必须只包含 typed DetailRef")
            self._files.path(ref, storage_owner=storage_owner)
        removed: list[DetailRef] = []
        for ref in sorted(
            set(refs), key=lambda item: (item.assembly_id, item.detail_id)
        ):
            paths = self._files.remove_paths(ref, storage_owner=storage_owner)
            if not paths:
                continue
            manifest = self._files.path(ref, storage_owner=storage_owner)
            if manifest is not None and manifest in paths:
                _payload, record = parse_detail_payload(
                    self._files.read(
                        ref, session_id=session_id, thread_id=thread_id
                    ),
                    detail_ref=ref,
                )
                expires = expiry_time(record.expires_at)
                if expires is None or expires > expired_before:
                    continue
            # manifest 已缺失时，owner 的 tombstone 仍授权删除该 ref 的残留正文文件。
            if self._remove_ref(ref, storage_owner=storage_owner):
                removed.append(ref)
        return tuple(removed)


__all__ = ["DetailRemoval"]
