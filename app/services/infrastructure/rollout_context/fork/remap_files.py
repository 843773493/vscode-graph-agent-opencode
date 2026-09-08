"""full_rollout_copy 的 JSONL/detail 文件物化与失败恢复。"""

from __future__ import annotations

import os
from pathlib import Path

from app.domain.itemized.hashing import canonical_json_bytes, sha256_jcs
from app.services.infrastructure.rollout_context.assembly.detail_identity import (
    detail_ref_from_key,
)
from app.services.infrastructure.rollout_context.fork.remap_state import (
    FullCopyRemapState,
)
from app.services.infrastructure.rollout_context.fork.validation import (
    required_text,
)
from app.services.infrastructure.rollout_context.runtime.detail_manifest import (
    detail_relative_path,
)
from app.services.infrastructure.rollout_context.runtime.detail_payload import (
    parse_detail_payload,
)


def materialize_full_copy_files(state: FullCopyRemapState) -> None:
    self = state.service
    connection = state.connection
    target_session_id = state.target_session_id
    fork_id = state.fork_id
    checkpoint_ns = state.checkpoint_ns
    new_lines = state.new_lines
    mapped = state.mapped
    jsonl_path = self.jsonl_path(target_session_id, checkpoint_ns)
    temporary = jsonl_path.with_name(f".{jsonl_path.name}.{fork_id}.tmp")
    # 第一处物理写入前记录恢复路径，detail 中途失败也必须恢复 JSONL。
    state.jsonl_path = jsonl_path
    state.temporary = temporary
    moved_details = []
    state.moved_details = moved_details
    with temporary.open("xb") as stream:
        for line in new_lines:
            stream.write(line)
        stream.flush()
        os.fsync(stream.fileno())

    temporary.replace(jsonl_path)

    if state.detail_capability is not None:
        from app.services.infrastructure.rollout_context.fork.full_copy.details import (
            materialize_details,
        )

        materialize_details(state)
        return

    detail_rows = tuple(
        connection.execute(
            "SELECT detail_ref, assembly_id, relative_path FROM context_plan_details"
        ).fetchall()
    )

    session_root = self.root(target_session_id, checkpoint_ns).parent

    for old_detail_ref, old_assembly_id, old_relative_path in detail_rows:
        old_detail_ref = required_text(
            old_detail_ref, field="context_plan_details.detail_ref"
        )
        old_assembly_id = required_text(
            old_assembly_id, field="context_plan_details.assembly_id"
        )
        relative_path_text = required_text(
            old_relative_path,
            field=f"context_plan_details.relative_path:{old_detail_ref}",
        )
        old_relative = Path(relative_path_text)
        source_ref = detail_ref_from_key(old_detail_ref)
        source_ref.require_owner(state.source_session_id, old_assembly_id)
        if relative_path_text != detail_relative_path(source_ref).as_posix():
            raise RuntimeError(f"full_rollout_copy detail path 越界: {old_detail_ref}")
        old_path = self._safe_session_relative_path(session_root, old_relative)
        new_detail_ref = mapped("detail", old_detail_ref)
        new_assembly_id = mapped("assembly", old_assembly_id)
        if new_detail_ref is None or new_assembly_id is None:
            raise RuntimeError(
                "full_rollout_copy detail target identity mapping 缺失: "
                f"{old_detail_ref}"
            )
        target_ref = detail_ref_from_key(new_detail_ref)
        target_ref.require_owner(state.target_session_id, new_assembly_id)
        new_relative = detail_relative_path(target_ref)
        self._safe_session_relative_path(session_root, new_relative.parent)
        new_path = session_root / new_relative
        if old_path.exists():
            if old_path.is_symlink() or not old_path.is_file():
                raise RuntimeError(
                    f"full_rollout_copy detail source 不是安全普通文件: {old_path}"
                )
            new_path.parent.mkdir(parents=True, exist_ok=True)
            new_path = self._safe_session_relative_path(session_root, new_relative)
            if new_path.exists() or new_path.is_symlink():
                raise RuntimeError(
                    f"full_rollout_copy detail target 已存在: {new_path}"
                )
            old_path.replace(new_path)
            moved_details.append((new_path, old_path))
            original = new_path.read_bytes()
            state.detail_originals[old_path] = original
            envelope, source_record = parse_detail_payload(
                original, detail_ref=source_ref
            )
            if source_record.protection == "protected":
                raise RuntimeError(
                    "detail-unavailable: protected detail 无 target owner 重封装能力"
                )
            envelope["detail_ref"] = target_ref.to_dict()
            encoded = canonical_json_bytes(envelope)
            parse_detail_payload(encoded, detail_ref=target_ref)
            with new_path.open("wb") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            state.detail_hashes[new_detail_ref] = sha256_jcs(envelope)
    state.jsonl_path = jsonl_path
    state.temporary = temporary
    state.moved_details = moved_details
    state.detail_rows = detail_rows
    state.session_root = session_root


def restore_full_copy_files(state: FullCopyRemapState) -> None:
    if state.temporary is not None and state.temporary.exists():
        state.temporary.unlink()
    if state.jsonl_path is not None and state.jsonl_path.exists():
        with state.jsonl_path.open("wb") as stream:
            stream.write(state.old_jsonl)
            stream.flush()
            os.fsync(stream.fileno())
    for new_path, old_path in reversed(state.moved_details):
        if new_path.exists() and not old_path.exists():
            new_path.replace(old_path)
            original = state.detail_originals.get(old_path)
            if original is not None:
                with old_path.open("wb") as stream:
                    stream.write(original)
                    stream.flush()
                    os.fsync(stream.fileno())
