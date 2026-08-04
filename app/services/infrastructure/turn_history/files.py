from __future__ import annotations

import hashlib
import os
import shutil
import threading
from pathlib import Path
from typing import BinaryIO, Literal

from app.core.path_utils import get_session_path_resolver
from app.schemas.public_v2.turn import TurnDetailDTO

from .models import TurnIndex, TurnManifest, TurnRecord, TurnRecordHeader
from .summary import to_turn_summary

MAX_TURN_HEADER_BYTES = 64 * 1024


class TurnHistoryFiles:
    """解析会话节点路径并提供 Turn 文件格式的原子读写。"""

    def __init__(
        self,
        sessions_dir: Path,
        *,
        directory_name: str,
        write_durability: Literal["immediate", "publish"],
    ) -> None:
        if (
            not directory_name
            or directory_name in {".", ".."}
            or "/" in directory_name
            or "\\" in directory_name
        ):
            raise ValueError(f"Turn history 目录名非法: {directory_name!r}")
        self._path_resolver = get_session_path_resolver(sessions_dir)
        self.directory_name = directory_name
        self._write_durability = write_durability

    def load_or_initialize(
        self,
        session_id: str,
    ) -> tuple[TurnManifest, TurnIndex]:
        root = self.root(session_id)
        root.mkdir(parents=True, exist_ok=True)
        self.turns_dir(session_id).mkdir(parents=True, exist_ok=True)
        manifest_path = self.manifest_path(session_id)
        index_path = self.index_path(session_id)
        if manifest_path.exists() != index_path.exists():
            raise RuntimeError(
                "Turn manifest/index 必须同时存在: "
                f"session_id={session_id}, root={root}"
            )
        if not manifest_path.exists():
            manifest = TurnManifest()
            index = TurnIndex()
            self.atomic_write_bytes(self.timeline_path(session_id), b"")
            self.atomic_write_bytes(
                self.operations_path(
                    session_id,
                    generation=manifest.operation_generation,
                ),
                b"",
            )
            self.write_index(session_id, index)
            self.write_manifest(session_id, manifest)
            return manifest, index

        manifest = TurnManifest.model_validate_json(
            manifest_path.read_text(encoding="utf-8")
        )
        index = TurnIndex.model_validate_json(index_path.read_text(encoding="utf-8"))
        if manifest.projection_epoch != index.projection_epoch:
            raise RuntimeError(
                "Turn manifest/index epoch 不一致: "
                f"session_id={session_id}, manifest={manifest.projection_epoch}, "
                f"index={index.projection_epoch}"
            )
        if manifest.status == "failed":
            raise RuntimeError(
                "Turn 投影处于失败状态: "
                f"session_id={session_id}, error={manifest.error}"
            )
        return manifest, index

    def read_turn_record(
        self,
        session_id: str,
        turn_id: str,
        *,
        required: bool,
    ) -> TurnRecord | None:
        path = self.turn_record_path(session_id, turn_id)
        if not path.is_file():
            if required:
                raise KeyError(
                    f"Turn 不存在: session_id={session_id}, turn_id={turn_id}"
                )
            return None
        with path.open("rb") as stream:
            header = self._read_turn_header_line(stream, path=path)
            payload = stream.read()
        if not payload:
            raise RuntimeError(f"Turn 文件缺少 detail 段: {path}")
        turn = TurnDetailDTO.model_validate_json(payload)
        if turn.turn_id != turn_id or turn.session_id != session_id:
            raise RuntimeError(
                "Turn 文件身份与请求不一致: "
                f"session_id={session_id}, turn_id={turn_id}, path={path}"
            )
        if to_turn_summary(turn) != header.summary:
            raise RuntimeError(f"Turn header/detail 内容不一致: {path}")
        return TurnRecord(
            turn=turn,
            visible=header.visible,
            timeline_start=header.timeline_start,
            timeline_end=header.timeline_end,
            last_applied_event_id=header.last_applied_event_id,
        )

    def read_turn_header(
        self,
        session_id: str,
        turn_id: str,
        *,
        required: bool,
    ) -> TurnRecordHeader | None:
        path = self.turn_record_path(session_id, turn_id)
        if not path.is_file():
            if required:
                raise KeyError(
                    f"Turn 不存在: session_id={session_id}, turn_id={turn_id}"
                )
            return None
        with path.open("rb") as stream:
            header = self._read_turn_header_line(stream, path=path)
        if (
            header.summary.turn_id != turn_id
            or header.summary.session_id != session_id
        ):
            raise RuntimeError(
                "Turn header 身份与请求不一致: "
                f"session_id={session_id}, turn_id={turn_id}, path={path}"
            )
        return header

    def write_turn_record(self, session_id: str, record: TurnRecord) -> None:
        header = TurnRecordHeader(
            summary=to_turn_summary(record.turn),
            visible=record.visible,
            timeline_start=record.timeline_start,
            timeline_end=record.timeline_end,
            last_applied_event_id=record.last_applied_event_id,
        )
        header_line = header.model_dump_json().encode("utf-8") + b"\n"
        if len(header_line) > MAX_TURN_HEADER_BYTES:
            raise RuntimeError(
                "Turn header 超过固定读取上限: "
                f"turn_id={record.turn.turn_id}, bytes={len(header_line)}"
            )
        self.atomic_write_bytes(
            self.turn_record_path(session_id, record.turn.turn_id),
            header_line + record.turn.model_dump_json().encode("utf-8"),
        )

    def hide_turn_record(
        self,
        session_id: str,
        turn_id: str,
        *,
        event_id: str,
    ) -> TurnRecordHeader:
        """只改写有界 header，流式复制 detail，避免回退时解析大型 Turn。"""
        path = self.turn_record_path(session_id, turn_id)
        if not path.is_file():
            raise KeyError(f"Turn 不存在: session_id={session_id}, turn_id={turn_id}")
        temp_path = path.with_name(
            f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
        )
        with path.open("rb") as source:
            current = self._read_turn_header_line(source, path=path)
            hidden = current.model_copy(
                update={"visible": False, "last_applied_event_id": event_id}
            )
            header_line = hidden.model_dump_json().encode("utf-8") + b"\n"
            if len(header_line) > MAX_TURN_HEADER_BYTES:
                raise RuntimeError(f"Turn header 超过固定读取上限: {path}")
            with temp_path.open("wb") as target:
                target.write(header_line)
                shutil.copyfileobj(source, target, length=1024 * 1024)
                self.flush_stream(target)
        os.replace(temp_path, path)
        return current

    @staticmethod
    def _read_turn_header_line(stream: BinaryIO, *, path: Path) -> TurnRecordHeader:
        line = stream.readline(MAX_TURN_HEADER_BYTES + 1)
        if not line.endswith(b"\n") or len(line) > MAX_TURN_HEADER_BYTES:
            raise RuntimeError(f"Turn header 缺失或超过固定上限: {path}")
        return TurnRecordHeader.model_validate_json(line)

    def write_manifest(self, session_id: str, manifest: TurnManifest) -> None:
        self.atomic_write_bytes(
            self.manifest_path(session_id),
            manifest.model_dump_json().encode("utf-8"),
        )

    def write_index(self, session_id: str, index: TurnIndex) -> None:
        self.atomic_write_bytes(
            self.index_path(session_id),
            index.model_dump_json().encode("utf-8"),
        )

    def discard(self, session_id: str) -> None:
        root = self.root(session_id)
        if root.exists():
            shutil.rmtree(root)

    def recover_publish_root(self, session_id: str) -> bool:
        """恢复 staging 发布的两次 rename 中断状态。"""
        root = self.root(session_id)
        backup = self.publish_backup_path(session_id)
        if not root.exists() and backup.exists():
            os.rename(backup, root)
            return False
        return root.exists() and backup.exists()

    def finish_publish_recovery(self, session_id: str) -> None:
        """仅在新 authoritative 已完整验证后清理旧发布备份。"""
        root = self.root(session_id)
        backup = self.publish_backup_path(session_id)
        if not root.is_dir() or not backup.is_dir():
            raise RuntimeError(
                "Turn publish recovery 状态不完整: "
                f"session_id={session_id}, root={root}, backup={backup}"
            )
        shutil.rmtree(backup)

    def atomic_write_bytes(self, path: Path, payload: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = path.with_name(
            f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
        )
        with temp_path.open("wb") as stream:
            stream.write(payload)
            self.flush_stream(stream)
        os.replace(temp_path, path)

    def flush_stream(self, stream: BinaryIO) -> None:
        stream.flush()
        if self._write_durability == "immediate":
            os.fsync(stream.fileno())

    def sync_tree(self, session_id: str) -> None:
        """在 staging 发布前一次性同步全部派生文件。"""
        root = self.root(session_id)
        if not root.is_dir():
            raise RuntimeError(f"Turn history 目录不存在: {root}")
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            with path.open("rb") as stream:
                os.fsync(stream.fileno())

    def root(self, session_id: str) -> Path:
        return (
            self._path_resolver.resolve_session_node(session_id) / self.directory_name
        )

    def turns_dir(self, session_id: str) -> Path:
        return self.root(session_id) / "turns"

    def manifest_path(self, session_id: str) -> Path:
        return self.root(session_id) / "manifest.json"

    def index_path(self, session_id: str) -> Path:
        return self.root(session_id) / "index.json"

    def timeline_path(self, session_id: str) -> Path:
        return self.root(session_id) / "timeline.jsonl"

    def operations_path(self, session_id: str, *, generation: int) -> Path:
        if generation < 1:
            raise ValueError("Turn operation generation 必须大于 0")
        return self.root(session_id) / f"operations.{generation:020d}.jsonl"

    def publish_backup_path(self, session_id: str) -> Path:
        root = self.root(session_id)
        return root.with_name(f".{root.name}.publish-backup")

    def turn_record_path(self, session_id: str, turn_id: str) -> Path:
        digest = hashlib.sha256(turn_id.encode("utf-8")).hexdigest()
        return self.turns_dir(session_id) / f"{digest}.json"
