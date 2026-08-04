from __future__ import annotations

import os
import shutil
import threading
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from uuid import uuid4

from app.abstractions.turn_history import (
    TurnHistoryStoreProtocol,
    TurnProjectionOperation,
    TurnProjectionPublicationConflict,
    TurnProjectionWatermark,
)
from app.schemas.public_v2.turn import (
    TurnDetailBatchDTO,
    TurnDetailDTO,
    TurnPageDTO,
    TurnSummaryDTO,
)

from .files import TurnHistoryFiles
from .models import TurnIndex, TurnManifest
from .operations import TurnOperationManager
from .reader import TurnHistoryReader
from .timeline import TurnTimeline


class TurnHistoryStore:
    """会话内 Job-centric Turn 展示投影的并发安全门面。"""

    def __init__(
        self,
        sessions_dir: Path,
        *,
        compaction_threshold: int = 1024,
        directory_name: str = "turn_history",
        write_durability: Literal["immediate", "publish"] = "immediate",
    ) -> None:
        if compaction_threshold < 1:
            raise ValueError("Turn operation 压实阈值必须大于 0")
        self._files = TurnHistoryFiles(
            sessions_dir,
            directory_name=directory_name,
            write_durability=write_durability,
        )
        self._sessions_dir = sessions_dir
        self._directory_name = directory_name
        self._timeline = TurnTimeline(self._files)
        self._operations = TurnOperationManager(self._files, self._timeline)
        self._reader = TurnHistoryReader(self._files, self._timeline)
        self._compaction_threshold = compaction_threshold
        self._locks: defaultdict[str, threading.RLock] = defaultdict(threading.RLock)

    def apply_operation(
        self,
        session_id: str,
        operation: TurnProjectionOperation,
    ) -> bool:
        if not operation.mutations and not operation.hidden_turn_ids:
            raise ValueError("Turn projection operation 不能是空操作")
        with self._locks[session_id]:
            manifest, index = self._load_recovered(session_id)
            if self._operation_is_replay(manifest, operation):
                return False
            operation_end = self._operations.append(
                session_id,
                manifest,
                operation,
            )
            self._operations.apply(session_id, operation, index)
            manifest.applied_operation_offset = operation_end
            manifest.operation_count += 1
            self._apply_event_watermark(manifest, operation)
            manifest.updated_at = datetime.now(UTC)
            self._files.write_manifest(session_id, manifest)
            if manifest.operation_count >= self._compaction_threshold:
                self._operations.compact(session_id, manifest)
            return True

    def get_turn(self, session_id: str, turn_id: str) -> TurnDetailDTO | None:
        with self._locks[session_id]:
            self._load_recovered(session_id)
            record = self._files.read_turn_record(
                session_id,
                turn_id,
                required=False,
            )
            return record.turn if record is not None and record.visible else None

    def is_event_applied(
        self,
        session_id: str,
        turn_id: str,
        event_id: str,
    ) -> bool:
        with self._locks[session_id]:
            self._load_recovered(session_id)
            record = self._files.read_turn_header(
                session_id,
                turn_id,
                required=False,
            )
            return record is not None and event_id == record.last_applied_event_id

    def get_details(
        self,
        session_id: str,
        turn_ids: list[str],
    ) -> TurnDetailBatchDTO:
        with self._locks[session_id]:
            manifest, _ = self._load_recovered(session_id)
            return self._reader.get_details(
                session_id,
                turn_ids,
                manifest=manifest,
            )

    def latest_summary(self, session_id: str) -> TurnSummaryDTO | None:
        page = self.list_summaries(session_id, limit=1, cursor=None)
        return page.items[0] if page.items else None

    def list_summaries(
        self,
        session_id: str,
        *,
        limit: int = 20,
        cursor: str | None = None,
    ) -> TurnPageDTO:
        with self._locks[session_id]:
            manifest, index = self._load_recovered(session_id)
            return self._reader.list_summaries(
                session_id,
                manifest=manifest,
                index=index,
                limit=limit,
                cursor=cursor,
            )

    def projection_epoch(self, session_id: str) -> int:
        with self._locks[session_id]:
            manifest, _ = self._load_recovered(session_id)
            return manifest.projection_epoch

    def publication_watermark(self, session_id: str) -> TurnProjectionWatermark:
        with self._locks[session_id]:
            manifest, _ = self._load_recovered(session_id)
            return TurnProjectionWatermark(
                event_id=manifest.last_event_id,
                source_offset=manifest.last_event_offset,
                projection_epoch=manifest.projection_epoch,
            )

    def visible_turn_ids_from_message(
        self,
        session_id: str,
        message_id: str,
    ) -> list[str]:
        if not message_id:
            raise ValueError("回退目标 message_id 不能为空")
        with self._locks[session_id]:
            manifest, index = self._load_recovered(session_id)
            cursor: str | None = None
            suffix_newest_first: list[str] = []
            while True:
                page = self._reader.list_summaries(
                    session_id,
                    manifest=manifest,
                    index=index,
                    limit=20,
                    cursor=cursor,
                )
                for summary in page.items:
                    suffix_newest_first.append(summary.turn_id)
                    summary_message_ids = {
                        *summary.source_message_ids,
                        *(message.message_id for message in summary.user_messages),
                    }
                    target_found = message_id in summary_message_ids
                    if (
                        not target_found
                        and (summary.sources_truncated or summary.user_messages_truncated)
                    ):
                        record = self._files.read_turn_record(
                            session_id,
                            summary.turn_id,
                            required=True,
                        )
                        target_found = any(
                            message.message_id == message_id
                            for message in record.turn.user_messages
                        )
                    if target_found:
                        return list(reversed(suffix_newest_first))
                if not page.has_more or page.next_cursor is None:
                    return []
                cursor = page.next_cursor

    def truncate_from_message(self, session_id: str, message_id: str) -> int:
        """原子隐藏目标消息所在 Turn 及其后缀，并使旧分页 cursor 失效。"""
        if not self.projection_exists(session_id):
            return 0
        with self._locks[session_id]:
            manifest, _ = self._load_recovered(session_id)
            hidden_turn_ids = self.visible_turn_ids_from_message(session_id, message_id)
            if not hidden_turn_ids:
                return 0

            staging = TurnHistoryStore(
                self._sessions_dir,
                compaction_threshold=self._compaction_threshold,
                directory_name=f".{self._directory_name}-replay-staging",
                write_durability="publish",
            )
            staging.discard_projection(session_id)
            shutil.copytree(
                self._files.root(session_id),
                staging._files.root(session_id),
            )
            staging.apply_operation(
                session_id,
                TurnProjectionOperation(
                    event_id=f"session_turn_replay:{uuid4()}",
                    hidden_turn_ids=hidden_turn_ids,
                ),
            )
            staging.set_projection_status(session_id, "ready")
            self.publish_staging(
                session_id,
                staging,
                publication_base=TurnProjectionWatermark(
                    event_id=manifest.last_event_id,
                    source_offset=manifest.last_event_offset,
                    projection_epoch=manifest.projection_epoch,
                ),
            )
            return len(hidden_turn_ids)

    def projection_version(self, session_id: str) -> int:
        with self._locks[session_id]:
            manifest, _ = self._load_recovered(session_id)
            return manifest.projection_version

    def event_cursor(self, session_id: str) -> str | None:
        with self._locks[session_id]:
            manifest, _ = self._load_recovered(session_id)
            return manifest.last_event_id

    def event_offset(self, session_id: str) -> int:
        with self._locks[session_id]:
            manifest, _ = self._load_recovered(session_id)
            return manifest.last_event_offset

    def advance_event_cursor(
        self,
        session_id: str,
        event_id: str,
        *,
        source_offset: int,
    ) -> None:
        if not event_id:
            raise ValueError("Turn event cursor 不能为空")
        with self._locks[session_id]:
            manifest, _ = self._load_recovered(session_id)
            if source_offset < manifest.last_event_offset:
                return
            if (
                source_offset == manifest.last_event_offset
                and manifest.last_event_id not in {None, event_id}
            ):
                raise RuntimeError(
                    "Turn cursor offset 对应不同事件: "
                    f"session_id={session_id}, offset={source_offset}, "
                    f"current={manifest.last_event_id}, incoming={event_id}"
                )
            manifest.last_event_offset = source_offset
            manifest.last_event_id = event_id
            manifest.updated_at = datetime.now(UTC)
            self._files.write_manifest(session_id, manifest)

    def projection_exists(self, session_id: str) -> bool:
        return self._files.manifest_path(session_id).is_file()

    def projection_status(self, session_id: str) -> str:
        with self._locks[session_id]:
            manifest, _ = self._load_recovered(session_id)
            return manifest.status

    def history_initialized(self, session_id: str) -> bool:
        with self._locks[session_id]:
            manifest, _ = self._load_recovered(session_id)
            return manifest.history_initialized

    def mark_history_initialized(
        self,
        session_id: str,
        *,
        projection_version: int,
    ) -> None:
        if projection_version < 1:
            raise ValueError("Turn 投影版本必须是正整数")
        with self._locks[session_id]:
            manifest, _ = self._load_recovered(session_id)
            manifest.history_initialized = True
            manifest.projection_version = projection_version
            manifest.updated_at = datetime.now(UTC)
            self._files.write_manifest(session_id, manifest)

    def set_projection_status(
        self,
        session_id: str,
        status: Literal["ready", "partial", "failed"],
        *,
        error: str | None = None,
    ) -> None:
        if status == "failed" and not error:
            raise ValueError("失败 Turn 投影必须记录错误原因")
        if status != "failed" and error is not None:
            raise ValueError("非失败 Turn 投影不能携带错误原因")
        with self._locks[session_id]:
            manifest, _ = self._load_recovered(session_id)
            manifest.status = status
            manifest.error = error
            manifest.updated_at = datetime.now(UTC)
            self._files.write_manifest(session_id, manifest)

    def next_ordinal(self, session_id: str) -> int:
        with self._locks[session_id]:
            _, index = self._load_recovered(session_id)
            return index.latest_ordinal + 1

    def turn_count(self, session_id: str) -> int:
        with self._locks[session_id]:
            _, index = self._load_recovered(session_id)
            return index.turn_count

    def rebase(self, session_id: str) -> int:
        """用空 staging 原子替换派生 Turn；checkpoint 不受影响。"""
        with self._locks[session_id]:
            manifest, _ = self._load_recovered(session_id)
            staging = self.create_rebuild_staging(session_id)
            if manifest.last_event_id is not None:
                staging.advance_event_cursor(
                    session_id,
                    manifest.last_event_id,
                    source_offset=manifest.last_event_offset,
                )
            staging.set_projection_status(session_id, "ready")
            return self.publish_staging(
                session_id,
                staging,
                publication_base=TurnProjectionWatermark(
                    event_id=manifest.last_event_id,
                    source_offset=manifest.last_event_offset,
                    projection_epoch=manifest.projection_epoch,
                ),
            )

    def create_rebuild_staging(
        self,
        session_id: str,
    ) -> TurnHistoryStoreProtocol:
        staging = TurnHistoryStore(
            self._sessions_dir,
            compaction_threshold=self._compaction_threshold,
            directory_name=f".{self._directory_name}-rebuild-staging",
            write_durability="publish",
        )
        staging.discard_projection(session_id)
        staging.projection_epoch(session_id)
        return staging

    def compact(self, session_id: str) -> None:
        with self._locks[session_id]:
            manifest, _ = self._load_recovered(session_id)
            self._operations.compact(session_id, manifest)

    def discard_projection(self, session_id: str) -> None:
        with self._locks[session_id]:
            self._files.discard(session_id)

    def publish_staging(
        self,
        session_id: str,
        staging: TurnHistoryStoreProtocol,
        *,
        publication_base: TurnProjectionWatermark | None = None,
    ) -> int:
        if not isinstance(staging, TurnHistoryStore):
            raise TypeError("staging 必须是 TurnHistoryStore")
        if staging is self:
            raise ValueError("不能把权威 Turn store 自身作为 staging 发布")
        with self._locks[session_id], staging._locks[session_id]:
            current_manifest, _ = self._load_recovered(session_id)
            staging_manifest, staging_index = staging._load_recovered(session_id)
            if staging_manifest.status != "ready":
                raise RuntimeError(
                    "拒绝发布未就绪 Turn staging: "
                    f"session_id={session_id}, status={staging_manifest.status}"
                )
            expected = publication_base or TurnProjectionWatermark(
                event_id=staging_manifest.last_event_id,
                source_offset=staging_manifest.last_event_offset,
                projection_epoch=current_manifest.projection_epoch,
            )
            if current_manifest.projection_epoch != expected.projection_epoch:
                raise TurnProjectionPublicationConflict(
                    "Turn staging 发布时投影 epoch 已变化: "
                    f"session_id={session_id}, current={current_manifest.projection_epoch}, "
                    f"expected={expected.projection_epoch}"
                )
            if current_manifest.last_event_id != expected.event_id:
                raise TurnProjectionPublicationConflict(
                    "Turn staging 发布时事件水位已变化: "
                    f"session_id={session_id}, current={current_manifest.last_event_id}, "
                    f"expected={expected.event_id}, staging={staging_manifest.last_event_id}"
                )
            if current_manifest.last_event_offset != expected.source_offset:
                raise TurnProjectionPublicationConflict(
                    "Turn staging 发布时 source offset 已变化: "
                    f"session_id={session_id}, "
                    f"current={current_manifest.last_event_offset}, "
                    f"expected={expected.source_offset}, "
                    f"staging={staging_manifest.last_event_offset}"
                )

            next_epoch = current_manifest.projection_epoch + 1
            staging_manifest.projection_epoch = next_epoch
            staging_manifest.updated_at = datetime.now(UTC)
            staging_index.projection_epoch = next_epoch
            staging._files.write_index(session_id, staging_index)
            staging._files.write_manifest(session_id, staging_manifest)
            staging._files.sync_tree(session_id)

            current_root = self._files.root(session_id)
            staging_root = staging._files.root(session_id)
            backup_root = self._files.publish_backup_path(session_id)
            if backup_root.exists():
                raise RuntimeError(f"Turn staging 发布备份目录已存在: {backup_root}")
            os.rename(current_root, backup_root)
            try:
                os.rename(staging_root, current_root)
            except BaseException:
                os.rename(backup_root, current_root)
                raise
            shutil.rmtree(backup_root)
            return next_epoch

    def _load_recovered(
        self,
        session_id: str,
    ) -> tuple[TurnManifest, TurnIndex]:
        has_publish_backup = self._files.recover_publish_root(session_id)
        manifest, index = self._files.load_or_initialize(session_id)
        manifest, index = self._operations.recover(session_id, manifest, index)
        self._operations.remove_inactive_logs(
            session_id,
            active_generation=manifest.operation_generation,
        )
        if has_publish_backup:
            self._files.finish_publish_recovery(session_id)
        return manifest, index

    @staticmethod
    def _operation_is_replay(
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
            "Turn operation source offset 对应不同事件: "
            f"offset={source_offset}, current={manifest.last_event_id}, "
            f"incoming={operation.event_id}"
        )

    @staticmethod
    def _apply_event_watermark(
        manifest: TurnManifest,
        operation: TurnProjectionOperation,
    ) -> None:
        source_offset = operation.source_event_offset
        if source_offset is None or source_offset < manifest.last_event_offset:
            return
        if (
            source_offset == manifest.last_event_offset
            and manifest.last_event_id not in {None, operation.event_id}
        ):
            raise RuntimeError(
                "Turn operation source offset 对应不同事件: "
                f"offset={source_offset}, current={manifest.last_event_id}, "
                f"incoming={operation.event_id}"
            )
        manifest.last_event_offset = source_offset
        manifest.last_event_id = operation.event_id
