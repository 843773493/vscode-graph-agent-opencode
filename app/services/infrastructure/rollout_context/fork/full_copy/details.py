"""只在私有 staging 中写入已认证的 target-local detail。"""

from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path

from app.services.infrastructure.rollout_context.assembly.detail_identity import (
    detail_ref_from_key,
)
from app.services.infrastructure.rollout_context.assembly.detail_registry import (
    _COLUMNS,
    _detail_mapping,
)
from app.services.infrastructure.rollout_context.fork.remap_state import (
    FullCopyRemapState,
)
from app.services.infrastructure.rollout_context.runtime.detail_manifest import (
    DetailUnavailableError,
    detail_record_from_mapping,
    detail_relative_path,
    protected_detail_relative_path,
)


def write_private(path: Path, raw: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with os.fdopen(
        os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb"
    ) as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())


def materialize_details(state: FullCopyRemapState) -> None:
    capability = state.detail_capability
    if capability is None or state.target_session_key is None:
        raise DetailUnavailableError(
            "detail-forbidden: fork staging 缺少显式 capability/key"
        )
    cursor = state.connection.execute(
        f"SELECT {','.join(_COLUMNS)} FROM context_plan_details ORDER BY detail_ref"
    )
    raw_rows = cursor.fetchall()
    rows = [_detail_mapping(tuple(row)) for row in raw_rows]
    state.detail_rows = tuple((row[0], row[3], row[8]) for row in raw_rows)
    state.session_root = state.service.root(
        state.target_session_id, state.checkpoint_ns
    ).parent
    for raw, row in zip(raw_rows, rows, strict=True):
        source_key = raw[0]
        source = detail_record_from_mapping(row)
        source.detail_ref.require_owner(state.source_session_id)
        target_key = state.maps["detail"][source_key]
        target_ref = detail_ref_from_key(target_key)
        target_ref.require_owner(state.target_session_id)
        artifact = None
        try:
            artifact = capability.prepare_detail(
                source_record=source,
                target_ref=target_ref,
                target_session_key=state.target_session_key,
            )
        except DetailUnavailableError:
            if source.required:
                raise
        if artifact is None:
            # 无正文的 optional 记录保留审计 metadata，但绝不声称 target 可读。
            record = replace(
                source,
                session_id=target_ref.session_id,
                assembly_id=target_ref.assembly_id,
                detail_id=target_ref.detail_id,
                relative_path=detail_relative_path(target_ref).as_posix(),
                status="unavailable",
                availability="unavailable",
            )
        else:
            record = artifact.record
            write_private(
                state.session_root / detail_relative_path(target_ref),
                artifact.manifest_bytes,
            )
            if artifact.protected_bytes is not None:
                write_private(
                    state.session_root / protected_detail_relative_path(target_ref),
                    artifact.protected_bytes,
                )
            if source.redacted_stable_digest is not None:
                previous = state.detail_digests.setdefault(
                    source.redacted_stable_digest, record.redacted_stable_digest
                )
                if previous != record.redacted_stable_digest:
                    raise RuntimeError(
                        "source-mismatch: fork target digest mapping 冲突"
                    )
        state.detail_records[target_key] = record
        state.detail_hashes[target_key] = record.content_hash
