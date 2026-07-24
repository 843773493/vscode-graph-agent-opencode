"""用户可见消息投影：为会话历史提供最新页优先的游标读取。"""
from __future__ import annotations

import base64
import json
import os
import threading
from pathlib import Path
from typing import Sequence

from app.core.path_utils import get_session_path_resolver
from app.schemas.public_v2.common import CursorPage
from app.schemas.public_v2.message import MessageDTO


class StaleMessageHistoryCursorError(ValueError):
    """游标对应的消息版本已变化，调用方必须重新读取最新页。"""


class MessageHistoryStore:
    """保存可见消息 JSONL 与字节偏移索引，避免切换会话时读取完整 checkpoint。"""

    _SCHEMA_VERSION = 2

    def __init__(self, sessions_dir: str | Path) -> None:
        self._sessions_dir = Path(sessions_dir).resolve()
        self._path_resolver = get_session_path_resolver(self._sessions_dir)
        self._write_lock = threading.Lock()

    def latest_checkpoint_id(self, session_id: str) -> str:
        checkpoint_path = (
            self._path_resolver.resolve_session_dir(session_id)
            / "checkpoints"
            / "checkpoints.jsonl"
        )
        if not checkpoint_path.is_file():
            return ""
        last_line = self._read_last_nonempty_line(checkpoint_path)
        if not last_line:
            return ""
        record = json.loads(last_line)
        checkpoint_id = record.get("checkpoint_id")
        if not isinstance(checkpoint_id, str):
            raise TypeError(f"checkpoint 记录缺少 checkpoint_id: {checkpoint_path}")
        return checkpoint_id

    def is_current(self, session_id: str, checkpoint_id: str) -> bool:
        index = self._read_index(session_id, required=False)
        return index is not None and index["checkpoint_id"] == checkpoint_id

    def replace(
        self,
        session_id: str,
        checkpoint_id: str,
        messages: Sequence[MessageDTO],
    ) -> None:
        history_dir = (
            self._path_resolver.resolve_session_dir(session_id) / "message_history"
        )
        history_dir.mkdir(parents=True, exist_ok=True)
        data_path = history_dir / "visible_messages.jsonl"
        index_path = history_dir / "index.json"
        data_temp = history_dir / "visible_messages.jsonl.tmp"
        index_temp = history_dir / "index.json.tmp"

        offsets: list[int] = []
        roles: list[str] = []
        offset = 0
        with self._write_lock:
            with data_temp.open("wb") as stream:
                for message in messages:
                    encoded = (
                        json.dumps(
                            message.model_dump(mode="json"),
                            ensure_ascii=False,
                            separators=(",", ":"),
                        ).encode("utf-8")
                        + b"\n"
                    )
                    offsets.append(offset)
                    roles.append(message.role.value)
                    stream.write(encoded)
                    offset += len(encoded)
                stream.flush()
                os.fsync(stream.fileno())

            index = {
                "schema_version": self._SCHEMA_VERSION,
                "checkpoint_id": checkpoint_id,
                "count": len(messages),
                "offsets": offsets,
                "roles": roles,
            }
            index_temp.write_text(
                json.dumps(index, ensure_ascii=False, separators=(",", ":")),
                encoding="utf-8",
            )
            os.replace(data_temp, data_path)
            os.replace(index_temp, index_path)

    def page(
        self,
        session_id: str,
        checkpoint_id: str,
        *,
        limit: int,
        cursor: str | None,
    ) -> CursorPage[MessageDTO]:
        if limit < 1:
            raise ValueError("消息分页 limit 必须大于 0")
        index = self._read_index(session_id, required=True)
        if index["checkpoint_id"] != checkpoint_id:
            raise StaleMessageHistoryCursorError("消息历史已更新，请重新加载最新消息")

        count = index["count"]
        end = (
            count
            if cursor is None
            else self._decode_cursor(cursor, session_id, checkpoint_id)
        )
        if end < 0 or end > count:
            raise ValueError("消息历史游标位置无效")
        start = max(0, end - limit)
        roles = index["roles"]
        # 历史页从一轮用户输入开始，避免把 assistant 回复截成孤立半轮。
        while start > 0 and roles[start] != "user":
            start -= 1

        items = self._read_items(session_id, index["offsets"], start, end)
        return CursorPage(
            items=items,
            next_cursor=(
                self._encode_cursor(session_id, checkpoint_id, start)
                if start > 0
                else None
            ),
            has_more=start > 0,
        )

    def _read_index(self, session_id: str, *, required: bool) -> dict[str, object] | None:
        index_path = (
            self._path_resolver.resolve_session_dir(session_id)
            / "message_history"
            / "index.json"
        )
        if not index_path.is_file():
            if required:
                raise FileNotFoundError(f"消息历史索引不存在: {index_path}")
            return None
        raw = json.loads(index_path.read_text(encoding="utf-8"))
        if raw.get("schema_version") != self._SCHEMA_VERSION:
            if required:
                raise RuntimeError(f"不支持的消息历史索引版本: {raw.get('schema_version')}")
            return None
        checkpoint_id = raw.get("checkpoint_id")
        count = raw.get("count")
        offsets = raw.get("offsets")
        roles = raw.get("roles")
        if (
            not isinstance(checkpoint_id, str)
            or not isinstance(count, int)
            or not isinstance(offsets, list)
            or not all(isinstance(value, int) for value in offsets)
            or not isinstance(roles, list)
            or not all(isinstance(value, str) for value in roles)
            or count != len(offsets)
            or count != len(roles)
        ):
            raise TypeError(f"消息历史索引结构无效: {index_path}")
        return raw

    def _read_items(
        self,
        session_id: str,
        offsets: list[int],
        start: int,
        end: int,
    ) -> list[MessageDTO]:
        if start == end:
            return []
        data_path = (
            self._path_resolver.resolve_session_dir(session_id)
            / "message_history"
            / "visible_messages.jsonl"
        )
        with data_path.open("rb") as stream:
            stream.seek(offsets[start])
            byte_count = (
                offsets[end] - offsets[start]
                if end < len(offsets)
                else data_path.stat().st_size - offsets[start]
            )
            raw_page = stream.read(byte_count)
        records = [json.loads(line) for line in raw_page.splitlines() if line]
        return [MessageDTO.model_validate(record) for record in records]

    @staticmethod
    def _encode_cursor(session_id: str, checkpoint_id: str, before: int) -> str:
        payload = json.dumps(
            {
                "session_id": session_id,
                "checkpoint_id": checkpoint_id,
                "before": before,
            },
            separators=(",", ":"),
        ).encode("utf-8")
        return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")

    @staticmethod
    def _decode_cursor(cursor: str, session_id: str, checkpoint_id: str) -> int:
        try:
            padding = "=" * (-len(cursor) % 4)
            payload = json.loads(base64.urlsafe_b64decode(cursor + padding))
        except (ValueError, json.JSONDecodeError) as error:
            raise ValueError("消息历史游标格式无效") from error
        if (
            payload.get("session_id") != session_id
            or payload.get("checkpoint_id") != checkpoint_id
        ):
            raise StaleMessageHistoryCursorError("消息历史已更新，请重新加载最新消息")
        before = payload.get("before")
        if not isinstance(before, int):
            raise ValueError("消息历史游标缺少 before")
        return before

    @staticmethod
    def _read_last_nonempty_line(path: Path) -> bytes:
        with path.open("rb") as stream:
            stream.seek(0, os.SEEK_END)
            position = stream.tell()
            buffer = b""
            while position > 0:
                read_size = min(64 * 1024, position)
                position -= read_size
                stream.seek(position)
                buffer = stream.read(read_size) + buffer
                stripped = buffer.rstrip(b"\r\n")
                separator = stripped.rfind(b"\n")
                if separator >= 0 or position == 0:
                    return stripped[separator + 1 :]
            return b""
