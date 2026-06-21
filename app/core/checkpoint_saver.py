"""基于文件系统的 LangGraph CheckpointSaver，checkpoint 写入 .boxteam/checkpoints/。"""
from __future__ import annotations

import asyncio
import json
import os
import threading
from collections.abc import AsyncIterator, Iterator, Sequence
from contextlib import AbstractAsyncContextManager, AbstractContextManager
from pathlib import Path
from types import TracebackType
from typing import Any, Optional

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import (
    BaseCheckpointSaver,
    ChannelVersions,
    Checkpoint,
    CheckpointMetadata,
    CheckpointTuple,
    PendingWrite,
    get_checkpoint_id,
    get_checkpoint_metadata,
)
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer


class FileSystemCheckpointSaver(
    BaseCheckpointSaver[str],
    AbstractContextManager,
    AbstractAsyncContextManager,
):
    """将 LangGraph checkpoint 持久化到文件系统。

    存储布局：
        {base_dir}/{thread_id}/{checkpoint_ns}/checkpoints.jsonl
        {base_dir}/{thread_id}/{checkpoint_ns}/writes.jsonl
        {base_dir}/{thread_id}/{checkpoint_ns}/blobs/{channel}_{version}.bin

    当前实现每个 thread 只保存最新 checkpoint（保留 parent chain），
    因为项目使用 session_id 作为 thread_id 且每次运行需要快速恢复。
    """

    def __init__(self, base_dir: str | Path, *, serde: Any | None = None) -> None:
        super().__init__(serde=serde)
        self.base_dir = Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self._serde = serde or JsonPlusSerializer()
        self._lock = threading.Lock()

    def _thread_dir(self, thread_id: str) -> Path:
        return self.base_dir / thread_id

    def _ns_dir(self, thread_id: str, checkpoint_ns: str) -> Path:
        return self._thread_dir(thread_id) / checkpoint_ns

    def _checkpoints_file(self, thread_id: str, checkpoint_ns: str) -> Path:
        return self._ns_dir(thread_id, checkpoint_ns) / "checkpoints.jsonl"

    def _writes_file(self, thread_id: str, checkpoint_ns: str) -> Path:
        return self._ns_dir(thread_id, checkpoint_ns) / "writes.jsonl"

    def _blob_file(
        self,
        thread_id: str,
        checkpoint_ns: str,
        channel: str,
        version: str | int | float,
    ) -> Path:
        safe_channel = channel.replace(":", "_").replace("/", "_")
        safe_version = str(version).replace(":", "_").replace("/", "_")
        return self._ns_dir(thread_id, checkpoint_ns) / "blobs" / f"{safe_channel}_{safe_version}.bin"

    def _ensure_ns_dirs(self, thread_id: str, checkpoint_ns: str) -> None:
        self._ns_dir(thread_id, checkpoint_ns).mkdir(parents=True, exist_ok=True)
        (self._ns_dir(thread_id, checkpoint_ns) / "blobs").mkdir(parents=True, exist_ok=True)

    def _serialize_to_jsonl_record(self, value: Any) -> Any:
        """将任意值转换为可写入 JSONL 的记录格式。"""
        type_tag, payload = self._serde.dumps_typed(value)
        if type_tag == "json":
            return json.loads(payload.decode("utf-8"))
        return {
            "__serde_type__": type_tag,
            "__serde_payload__": payload.hex(),
        }

    def _deserialize_from_jsonl_record(self, record: Any) -> Any:
        """从 JSONL 记录格式还原任意值。"""
        if isinstance(record, dict) and "__serde_type__" in record:
            payload = bytes.fromhex(record["__serde_payload__"])
            return self._serde.loads_typed((record["__serde_type__"], payload))
        return record

    def _serialize_blob(self, value: Any) -> bytes:
        """序列化 blob 值，返回可写入文件的 bytes。"""
        type_tag, payload = self._serde.dumps_typed(value)
        if type_tag == "json":
            return payload
        return payload

    def _deserialize_blob(self, raw: bytes) -> Any:
        """从文件 bytes 反序列化 blob 值。"""
        if not raw:
            return ("empty", b"")
        try:
            decoded = raw.decode("utf-8")
            return json.loads(decoded)
        except (UnicodeDecodeError, json.JSONDecodeError):
            value = self._serde.loads_typed(("msgpack", raw))
            return self._decode_bytes(value)

    def _decode_bytes(self, value: Any) -> Any:
        """递归将 ormsgpack 反序列化后的 bytes 转换回 str。"""
        if isinstance(value, bytes):
            return value.decode("utf-8")
        if isinstance(value, str):
            return value
        if isinstance(value, list):
            return [self._decode_bytes(item) for item in value]
        if isinstance(value, tuple):
            return tuple(self._decode_bytes(item) for item in value)
        if isinstance(value, dict):
            return {
                self._decode_bytes(k): self._decode_bytes(v)
                for k, v in value.items()
            }
        return value

    def _load_blobs(
        self,
        thread_id: str,
        checkpoint_ns: str,
        versions: ChannelVersions,
    ) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for channel, version in versions.items():
            blob_path = self._blob_file(thread_id, checkpoint_ns, channel, version)
            if not blob_path.exists():
                continue
            with open(blob_path, "rb") as f:
                data = self._deserialize_blob(f.read())
            if isinstance(data, tuple) and len(data) == 2 and data[0] == "empty":
                continue
            if data == ("empty", b""):
                continue
            result[channel] = data
        return result

    def get_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        thread_id: str = config["configurable"]["thread_id"]
        checkpoint_ns: str = config["configurable"].get("checkpoint_ns", "")
        checkpoint_id = get_checkpoint_id(config)

        checkpoints_file = self._checkpoints_file(thread_id, checkpoint_ns)
        if not checkpoints_file.exists():
            return None

        latest_entry: tuple[str, Any, Any, Optional[str]] | None = None
        target_entry: tuple[str, Any, Any, Optional[str]] | None = None
        with open(checkpoints_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                entry = (
                    record["checkpoint_id"],
                    record["checkpoint"],
                    record["metadata"],
                    record.get("parent_checkpoint_id"),
                )
                latest_entry = entry
                if checkpoint_id and entry[0] == checkpoint_id:
                    target_entry = entry
                    break

        entry = target_entry or latest_entry
        if entry is None:
            return None

        cp_id, cp_dict, meta_dict, parent_id = entry
        checkpoint: Checkpoint = self._deserialize_from_jsonl_record(cp_dict)
        metadata: CheckpointMetadata = self._deserialize_from_jsonl_record(meta_dict)

        channel_values = self._load_blobs(
            thread_id,
            checkpoint_ns,
            checkpoint.get("channel_versions", {}),
        )
        checkpoint["channel_values"] = channel_values

        writes = self._load_writes(thread_id, checkpoint_ns, cp_id)

        return CheckpointTuple(
            config={
                "configurable": {
                    "thread_id": thread_id,
                    "checkpoint_ns": checkpoint_ns,
                    "checkpoint_id": cp_id,
                }
            },
            checkpoint=checkpoint,
            metadata=metadata,
            pending_writes=writes,
            parent_config=(
                {
                    "configurable": {
                        "thread_id": thread_id,
                        "checkpoint_ns": checkpoint_ns,
                        "checkpoint_id": parent_id,
                    }
                }
                if parent_id
                else None
            ),
        )

    def _load_writes(
        self,
        thread_id: str,
        checkpoint_ns: str,
        checkpoint_id: str,
    ) -> list[PendingWrite]:
        writes_file = self._writes_file(thread_id, checkpoint_ns)
        if not writes_file.exists():
            return []

        writes: list[PendingWrite] = []
        with open(writes_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if record.get("checkpoint_id") != checkpoint_id:
                    continue
                value = self._deserialize_from_jsonl_record(record["value"])
                writes.append((record["task_id"], record["channel"], value))
        return writes

    def list(
        self,
        config: RunnableConfig | None,
        *,
        filter: dict[str, Any] | None = None,
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> Iterator[CheckpointTuple]:
        thread_ids: tuple[str, ...]
        if config:
            thread_ids = (config["configurable"]["thread_id"],)
        else:
            thread_ids = tuple(
                d.name for d in self.base_dir.iterdir() if d.is_dir()
            )

        config_checkpoint_ns = (
            config["configurable"].get("checkpoint_ns", "") if config else None
        )
        config_checkpoint_id = get_checkpoint_id(config) if config else None
        before_checkpoint_id = get_checkpoint_id(before) if before else None

        for thread_id in thread_ids:
            thread_dir = self._thread_dir(thread_id)
            if not thread_dir.exists():
                continue
            for ns_dir in thread_dir.iterdir():
                if not ns_dir.is_dir():
                    continue
                checkpoint_ns = ns_dir.name
                if (
                    config_checkpoint_ns is not None
                    and checkpoint_ns != config_checkpoint_ns
                ):
                    continue

                checkpoints_file = ns_dir / "checkpoints.jsonl"
                if not checkpoints_file.exists():
                    continue

                with open(checkpoints_file, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        record = json.loads(line)
                        cp_id = record["checkpoint_id"]

                        if config_checkpoint_id and cp_id != config_checkpoint_id:
                            continue
                        if before_checkpoint_id and cp_id >= before_checkpoint_id:
                            continue

                        metadata = self._deserialize_from_jsonl_record(
                            record["metadata"]
                        )
                        if filter and not all(
                            query_value == metadata.get(query_key)
                            for query_key, query_value in filter.items()
                        ):
                            continue

                        if limit is not None and limit <= 0:
                            break
                        elif limit is not None:
                            limit -= 1

                        checkpoint = self._deserialize_from_jsonl_record(
                            record["checkpoint"]
                        )
                        checkpoint["channel_values"] = self._load_blobs(
                            thread_id,
                            checkpoint_ns,
                            checkpoint.get("channel_versions", {}),
                        )

                        yield CheckpointTuple(
                            config={
                                "configurable": {
                                    "thread_id": thread_id,
                                    "checkpoint_ns": checkpoint_ns,
                                    "checkpoint_id": cp_id,
                                }
                            },
                            checkpoint=checkpoint,
                            metadata=metadata,
                            parent_config=(
                                {
                                    "configurable": {
                                        "thread_id": thread_id,
                                        "checkpoint_ns": checkpoint_ns,
                                        "checkpoint_id": record.get(
                                            "parent_checkpoint_id"
                                        ),
                                    }
                                }
                                if record.get("parent_checkpoint_id")
                                else None
                            ),
                            pending_writes=self._load_writes(
                                thread_id, checkpoint_ns, cp_id
                            ),
                        )

    def put(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        thread_id: str = config["configurable"]["thread_id"]
        checkpoint_ns: str = config["configurable"].get("checkpoint_ns", "")
        self._ensure_ns_dirs(thread_id, checkpoint_ns)

        # 保存 blobs
        values = checkpoint.get("channel_values", {})
        for channel, version in new_versions.items():
            blob_path = self._blob_file(thread_id, checkpoint_ns, channel, version)
            if channel in values:
                data = self._serialize_blob(values[channel])
            else:
                data = self._serialize_blob(("empty", b""))
            with open(blob_path, "wb") as f:
                f.write(data)

        # checkpoint 本体不包含 channel_values（已拆出到 blobs）
        checkpoint_without_values = {
            k: v for k, v in checkpoint.items() if k != "channel_values"
        }

        parent_checkpoint_id = config["configurable"].get("checkpoint_id")
        record = {
            "checkpoint_id": checkpoint["id"],
            "checkpoint": self._serialize_to_jsonl_record(checkpoint_without_values),
            "metadata": self._serialize_to_jsonl_record(
                get_checkpoint_metadata(config, metadata)
            ),
            "parent_checkpoint_id": parent_checkpoint_id,
        }

        checkpoints_file = self._checkpoints_file(thread_id, checkpoint_ns)
        with self._lock:
            with open(checkpoints_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")

        return {
            "configurable": {
                "thread_id": thread_id,
                "checkpoint_ns": checkpoint_ns,
                "checkpoint_id": checkpoint["id"],
            }
        }

    def put_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        thread_id: str = config["configurable"]["thread_id"]
        checkpoint_ns: str = config["configurable"].get("checkpoint_ns", "")
        checkpoint_id: str = config["configurable"]["checkpoint_id"]
        self._ensure_ns_dirs(thread_id, checkpoint_ns)

        writes_file = self._writes_file(thread_id, checkpoint_ns)
        with self._lock:
            with open(writes_file, "a", encoding="utf-8") as f:
                for idx, (channel, value) in enumerate(writes):
                    record = {
                        "checkpoint_id": checkpoint_id,
                        "task_id": task_id,
                        "channel": channel,
                        "value": self._serialize_to_jsonl_record(value),
                        "task_path": task_path,
                        "idx": idx,
                    }
                    f.write(json.dumps(record, ensure_ascii=False) + "\n")

    def delete_thread(self, thread_id: str) -> None:
        thread_dir = self._thread_dir(thread_id)
        if thread_dir.exists():
            import shutil
            shutil.rmtree(thread_dir)

    async def aget_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        return await asyncio.get_running_loop().run_in_executor(
            None, self.get_tuple, config
        )

    async def alist(
        self,
        config: RunnableConfig | None,
        *,
        filter: dict[str, Any] | None = None,
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> AsyncIterator[CheckpointTuple]:
        for item in self.list(config, filter=filter, before=before, limit=limit):
            yield item

    async def aput(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        return await asyncio.get_running_loop().run_in_executor(
            None, self.put, config, checkpoint, metadata, new_versions
        )

    async def aput_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        await asyncio.get_running_loop().run_in_executor(
            None, self.put_writes, config, writes, task_id, task_path
        )

    async def adelete_thread(self, thread_id: str) -> None:
        await asyncio.get_running_loop().run_in_executor(None, self.delete_thread, thread_id)

    def get_next_version(self, current: str | None, channel: None) -> str:
        if current is None:
            current_v = 0
        elif isinstance(current, int):
            current_v = current
        else:
            current_v = int(current.split(".")[0])
        next_v = current_v + 1
        next_h = os.urandom(4).hex()
        return f"{next_v:032}.{next_h}"

    def __enter__(self) -> "FileSystemCheckpointSaver":
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        return None

    async def __aenter__(self) -> "FileSystemCheckpointSaver":
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        return None
