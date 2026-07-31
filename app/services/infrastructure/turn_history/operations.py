from __future__ import annotations

from datetime import UTC, datetime

from app.abstractions.turn_history import TurnProjectionOperation
from app.schemas.public_v2.turn import TurnDetailDTO

from .files import TurnHistoryFiles
from .models import TurnIndex, TurnManifest, TurnRecord
from .timeline import TurnTimeline


class TurnOperationManager:
    """追加、恢复并压实 Turn projection operations。"""

    def __init__(self, files: TurnHistoryFiles, timeline: TurnTimeline) -> None:
        self._files = files
        self._timeline = timeline

    def recover(
        self,
        session_id: str,
        manifest: TurnManifest,
        index: TurnIndex,
    ) -> tuple[TurnManifest, TurnIndex]:
        operation_path = self._files.operations_path(
            session_id,
            generation=manifest.operation_generation,
        )
        if not operation_path.is_file():
            raise RuntimeError(
                "Turn active operation generation 丢失: "
                f"session_id={session_id}, generation={manifest.operation_generation}"
            )
        operation_size = operation_path.stat().st_size
        if manifest.applied_operation_offset > operation_size:
            raise RuntimeError(
                "Turn operation 水位越过文件末尾: "
                f"session_id={session_id}, offset={manifest.applied_operation_offset}, "
                f"size={operation_size}"
            )
        timeline_size = self._files.timeline_path(session_id).stat().st_size
        if index.timeline_size > timeline_size:
            raise RuntimeError(
                "Turn timeline 索引越过文件末尾: "
                f"session_id={session_id}, size={index.timeline_size}"
            )
        if manifest.applied_operation_offset == operation_size:
            self._timeline.validate_index_anchor(session_id, index)
            return manifest, index

        with operation_path.open("rb") as stream:
            stream.seek(manifest.applied_operation_offset)
            while line := stream.readline():
                if not line.endswith(b"\n"):
                    raise RuntimeError(
                        "Turn operation 尾行不完整，拒绝猜测恢复: "
                        f"session_id={session_id}, offset={stream.tell()}"
                    )
                operation = TurnProjectionOperation.model_validate_json(line)
                if self._is_replayed_operation(manifest, operation):
                    manifest.applied_operation_offset = stream.tell()
                    manifest.operation_count += 1
                    continue
                index = self.apply(session_id, operation, index)
                manifest.applied_operation_offset = stream.tell()
                manifest.operation_count += 1
                if operation.source_event_offset is not None:
                    if operation.source_event_offset < manifest.last_event_offset:
                        continue
                    if (
                        operation.source_event_offset == manifest.last_event_offset
                        and manifest.last_event_id not in {None, operation.event_id}
                    ):
                        raise RuntimeError(
                            "Turn operation source offset 与既有 cursor 冲突: "
                            f"session_id={session_id}, "
                            f"offset={operation.source_event_offset}"
                        )
                    manifest.last_event_offset = operation.source_event_offset
                    manifest.last_event_id = operation.event_id
        manifest.updated_at = datetime.now(UTC)
        self._files.write_manifest(session_id, manifest)
        self._timeline.validate_index_anchor(session_id, index)
        return manifest, index

    @staticmethod
    def _is_replayed_operation(
        manifest: TurnManifest,
        operation: TurnProjectionOperation,
    ) -> bool:
        source_offset = operation.source_event_offset
        if source_offset is None or source_offset > manifest.last_event_offset:
            return False
        if source_offset < manifest.last_event_offset:
            return True
        if manifest.last_event_id == operation.event_id:
            return True
        raise RuntimeError(
            "Turn recovery source offset 与既有 cursor 冲突: "
            f"offset={source_offset}, current={manifest.last_event_id}, "
            f"incoming={operation.event_id}"
        )

    def append(
        self,
        session_id: str,
        manifest: TurnManifest,
        operation: TurnProjectionOperation,
    ) -> int:
        path = self._files.operations_path(
            session_id,
            generation=manifest.operation_generation,
        )
        line = operation.model_dump_json().encode("utf-8") + b"\n"
        with path.open("ab") as stream:
            stream.write(line)
            self._files.flush_stream(stream)
            return stream.tell()

    def apply(
        self,
        session_id: str,
        operation: TurnProjectionOperation,
        index: TurnIndex,
    ) -> TurnIndex:
        affected_turn_ids = {
            *(mutation.turn_id for mutation in operation.mutations),
            *operation.hidden_turn_ids,
        }
        existing_records = {
            turn_id: self._files.read_turn_record(
                session_id,
                turn_id,
                required=False,
            )
            for turn_id in affected_turn_ids
        }
        timeline_path = self._files.timeline_path(session_id)
        repair_index = index.timeline_size != timeline_path.stat().st_size or any(
            record is not None and operation.event_id == record.last_applied_event_id
            for record in existing_records.values()
        )

        for mutation in operation.mutations:
            existing = existing_records[mutation.turn_id]
            if mutation.create is not None and mutation.create.session_id != session_id:
                raise ValueError(
                    "Turn operation 跨会话写入: "
                    f"scope={session_id}, turn_session={mutation.create.session_id}"
                )
            if (
                existing is not None
                and operation.event_id == existing.last_applied_event_id
            ):
                continue
            if existing is None:
                if (
                    mutation.base_revision != 0
                    or mutation.create is None
                    or mutation.patch is not None
                ):
                    raise RuntimeError(
                        "Turn 创建 operation 前置条件无效: "
                        f"session_id={session_id}, turn_id={mutation.turn_id}, "
                        f"base_revision={mutation.base_revision}"
                    )
                turn = mutation.create
                if turn.revision != 1 or turn.turn_id != mutation.turn_id:
                    raise RuntimeError(
                        "Turn 创建快照身份或 revision 无效: "
                        f"turn_id={mutation.turn_id}, revision={turn.revision}"
                    )
                timeline_start, timeline_end = self._timeline.find_or_append_entry(
                    session_id,
                    turn_id=turn.turn_id,
                    ordinal=turn.ordinal,
                    indexed_size=index.timeline_size,
                )
                record = TurnRecord(
                    turn=turn,
                    timeline_start=timeline_start,
                    timeline_end=timeline_end,
                    last_applied_event_id=operation.event_id,
                )
                index.turn_count += 1
                index.latest_ordinal = max(index.latest_ordinal, turn.ordinal)
            else:
                if (
                    mutation.create is not None
                    or mutation.patch is None
                    or mutation.base_revision != existing.turn.revision
                ):
                    raise RuntimeError(
                        "Turn 更新 operation base revision 不匹配: "
                        f"turn_id={mutation.turn_id}, "
                        f"expected={existing.turn.revision}, "
                        f"actual={mutation.base_revision}"
                    )
                patch = mutation.patch
                patch_values = patch.model_dump(
                    mode="python",
                    exclude_unset=True,
                    exclude={"append_items"},
                )
                updated_turn = TurnDetailDTO.model_validate(
                    {
                        **existing.turn.model_dump(mode="python"),
                        **patch_values,
                        "items": [*existing.turn.items, *patch.append_items],
                    }
                )
                if updated_turn.ordinal != existing.turn.ordinal:
                    raise RuntimeError(
                        "Turn ordinal 不允许原位改变: "
                        f"turn_id={updated_turn.turn_id}, old={existing.turn.ordinal}, "
                        f"new={updated_turn.ordinal}"
                    )
                if updated_turn.revision != existing.turn.revision + 1:
                    raise RuntimeError(
                        "Turn revision 必须严格递增 1: "
                        f"turn_id={updated_turn.turn_id}, old={existing.turn.revision}, "
                        f"new={updated_turn.revision}"
                    )
                record = existing.model_copy(
                    update={
                        "turn": updated_turn,
                        "visible": True,
                        "last_applied_event_id": operation.event_id,
                    }
                )
                if not existing.visible:
                    index.turn_count += 1
            self._files.write_turn_record(session_id, record)

        for turn_id in operation.hidden_turn_ids:
            record = existing_records.get(turn_id)
            if record is None:
                raise KeyError(
                    f"待隐藏 Turn 不存在: session_id={session_id}, turn_id={turn_id}"
                )
            if operation.event_id == record.last_applied_event_id:
                continue
            if record.visible:
                index.turn_count -= 1
            hidden_record = record.model_copy(
                update={
                    "visible": False,
                    "last_applied_event_id": operation.event_id,
                }
            )
            self._files.write_turn_record(session_id, hidden_record)

        if repair_index:
            index = self._timeline.rebuild_index(
                session_id,
                projection_epoch=index.projection_epoch,
            )
        else:
            index.timeline_size = timeline_path.stat().st_size
            index.latest_turn_id = self._timeline.find_latest_visible_turn_id(
                session_id,
                index,
            )
        self._files.write_index(session_id, index)
        return index

    def compact(self, session_id: str, manifest: TurnManifest) -> None:
        current_generation = manifest.operation_generation
        next_generation = current_generation + 1
        next_path = self._files.operations_path(
            session_id,
            generation=next_generation,
        )
        if next_path.exists() and next_path.stat().st_size > 0:
            raise RuntimeError(
                "Turn 待启用 operation generation 含有未解释数据: "
                f"session_id={session_id}, generation={next_generation}"
            )
        self._files.atomic_write_bytes(next_path, b"")
        compacted_manifest = manifest.model_copy(
            update={
                "operation_generation": next_generation,
                "compacted_operation_count": (
                    manifest.compacted_operation_count + manifest.operation_count
                ),
                "operation_count": 0,
                "applied_operation_offset": 0,
                "updated_at": datetime.now(UTC),
            }
        )
        self._files.write_manifest(session_id, compacted_manifest)
        self.remove_inactive_logs(session_id, active_generation=next_generation)

    def remove_inactive_logs(
        self,
        session_id: str,
        *,
        active_generation: int,
    ) -> None:
        active_path = self._files.operations_path(
            session_id,
            generation=active_generation,
        )
        for path in self._files.root(session_id).glob("operations.*.jsonl"):
            if path != active_path:
                path.unlink()
