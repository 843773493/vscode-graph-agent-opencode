"""封存请求的不可变内容 DAG；历史正文只从已冻结 SQLite/JSONL 读取。"""

from __future__ import annotations

import json
import sqlite3
from copy import deepcopy
from pathlib import Path

from app.domain.itemized.hashing import canonical_json_bytes, sha256_jcs
from app.services.infrastructure.rollout_context.storage.catalog.history_scope import (
    read_local_scopes,
    require_historical_scope,
)
from app.services.infrastructure.rollout_context.storage.catalog.item_reader import (
    RolloutCanonicalReadMixin,
)

_FIELDS = ("request_hash_preimage", "tool_snapshot", "tool_set_refs", "seal_binding")
_FORMAT = "assembly_content_v1"


class _AssemblyContentStore:
    def __init__(self, connection: sqlite3.Connection, session: str, thread: str) -> None:
        require_historical_scope(connection, session, thread)
        self.connection = connection
        self.assembly_owner = (session, thread)
        self.nodes: dict[str, dict[str, object]] = {}
        self.values: dict[str, object] = {}
        self.canonical_payloads: dict[tuple[str, str, str], object] = {}
        self.sources: dict[str, dict[str, object]] = {}
        self.dependencies: set[str] = set()

    def _canonical_item_payload(
        self, canonical_item_id: str, historical_owner: tuple[str, str],
    ) -> object:
        require_historical_scope(self.connection, *historical_owner)
        canonical_identity = (*historical_owner, canonical_item_id)
        if canonical_identity in self.canonical_payloads:
            return self.canonical_payloads[canonical_identity]
        locator = self.connection.execute(
            "SELECT i.item_sequence,i.jsonl_offset,i.jsonl_length,e.session_id,e.thread_id,"
            "i.execution_id,i.turn_scope,i.turn_id "
            "FROM item_catalog i LEFT JOIN executions e ON e.execution_id=i.execution_id WHERE i.item_id=?",
            (canonical_item_id,),
        ).fetchone()
        if locator is None:
            raise ValueError(f"source-mismatch: assembly canonical owner/locator 不一致: {canonical_item_id}")
        # 仅发布事件没有 execution，归属由已核验的本地/冻结 namespace 与 item 身份共同约束。
        if ((locator[5] is not None and locator[3:5] != historical_owner)
                or (locator[5] is None and locator[6:] != ("ambient", None))):
            raise ValueError(f"source-mismatch: assembly canonical owner/locator 不一致: {canonical_item_id}")
        database_path = next(name for _, schema, name in self.connection.execute("PRAGMA database_list") if schema == "main")
        with Path(database_path).with_name("rollout.jsonl").open("rb") as stream:
            item = RolloutCanonicalReadMixin._read_v2_item_at(
                self.connection, stream, sequence=locator[0], offset=locator[1], length=locator[2],
            )
        self.canonical_payloads[canonical_identity] = item.payload
        self.dependencies.add(canonical_item_id)
        return item.payload

    def capture_sources(self, snapshot: dict[str, object]) -> None:
        def visit(value: object, path: list[str | int], reference: dict[str, object]) -> None:
            if isinstance(value, str):
                if value not in self.sources and len(value.encode("utf-8")) >= 128:
                    self.sources[value] = {
                        **reference,
                        "path": path.copy(),
                        "value_hash": sha256_jcs(value),
                    }
            elif isinstance(value, dict):
                for key, child in value.items():
                    path.append(key)
                    visit(child, path, reference)
                    path.pop()
            elif isinstance(value, list):
                for index, child in enumerate(value):
                    path.append(index)
                    visit(child, path, reference)
                    path.pop()

        local_scopes = read_local_scopes(self.connection)
        for selection in snapshot["selection"]:
            ref = selection["ref"]
            if not selection["included"] or ref["ref_type"] != "canonical_item":
                continue
            # 其它Thread的选择由既有manifest owner核验；这里只复用目标本地已捕获的正文。
            if (ref["session_id"], ref["thread_id"]) not in local_scopes or ref["content_length"] < 128:
                continue
            require_historical_scope(self.connection, ref["session_id"], ref["thread_id"])
            source = self.connection.execute(
                "SELECT i.content_hash,i.payload_length,i.source_revision,i.item_sequence,e.session_id,e.thread_id "
                "FROM item_catalog i LEFT JOIN executions e ON e.execution_id=i.execution_id WHERE i.item_id=?",
                (ref["ref_id"],),
            ).fetchone()
            if source is None or source[:4] != (ref["content_hash"], ref["content_length"], ref["source_revision"], ref["item_sequence"]):
                raise ValueError(f"source-mismatch: assembly canonical 版本不一致: {ref['ref_id']}")
            # fork 的本地 selection scope 与 immutable item 的历史 execution owner 分开。
            historical_owner = source[4:] if source[4] is not None else (ref["session_id"], ref["thread_id"])
            payload = self._canonical_item_payload(ref["ref_id"], historical_owner)
            visit(payload, [], {
                "item_id": ref["ref_id"],
                "session_id": historical_owner[0],
                "thread_id": historical_owner[1],
            })

    def _put(self, node: dict[str, object]) -> dict[str, str]:
        raw = canonical_json_bytes(node).decode("utf-8")
        key = sha256_jcs(node)
        if key not in self.nodes:
            self.connection.execute(
                "INSERT INTO assembly_content_nodes(session_id,thread_id,node_id,body_json) VALUES (?,?,?,?) "
                "ON CONFLICT(session_id,thread_id,node_id) DO NOTHING",
                (*self.assembly_owner, key, raw),
            )
            stored = self.connection.execute(
                "SELECT body_json FROM assembly_content_nodes WHERE session_id=? AND thread_id=? AND node_id=?",
                (*self.assembly_owner, key),
            ).fetchone()
            if stored is None or stored[0] != raw:
                raise ValueError(f"source-mismatch: assembly content 同版本异正文: {key}")
            self.nodes[key] = node
        return {"node": key}

    def encode(self, value: object) -> dict[str, object]:
        if isinstance(value, str) and value in self.sources:
            return self._put({"kind": "canonical", **self.sources[value]})
        raw = canonical_json_bytes(value)
        if len(raw) <= 128:
            return {"literal": value}
        if isinstance(value, dict):
            return self._put({"kind": "object", "entries": {key: self.encode(child) for key, child in value.items()}})
        if isinstance(value, list):
            prefix = None
            for child in value:
                prefix = self._put({"kind": "prefix", "parent": prefix, "value": self.encode(child)})
            return prefix if prefix is not None else {"literal": []}
        return self._put({"kind": "value", "value": value})

    def _node(self, key: str) -> dict[str, object]:
        if key not in self.nodes:
            row = self.connection.execute(
                "SELECT body_json FROM assembly_content_nodes WHERE session_id=? AND thread_id=? AND node_id=?",
                (*self.assembly_owner, key),
            ).fetchone()
            if row is None:
                raise ValueError(f"source-mismatch: assembly content 缺失: {self.assembly_owner}/{key}")
            node = json.loads(row[0])
            if (not isinstance(node, dict) or sha256_jcs(node) != key
                    or canonical_json_bytes(node).decode("utf-8") != row[0]):
                raise ValueError(f"source-mismatch: assembly content hash 不一致: {key}")
            self.nodes[key] = node
        return self.nodes[key]

    def decode(self, reference: dict[str, object]) -> object:
        if set(reference) == {"literal"}:
            return reference["literal"]
        if set(reference) != {"node"} or not isinstance(reference["node"], str):
            raise ValueError("source-mismatch: assembly content 引用非法")
        key = reference["node"]
        if key in self.values:
            return deepcopy(self.values[key])
        node = self._node(key)
        kind = node.get("kind")
        if kind == "value" and set(node) == {"kind", "value"}:
            value = node["value"]
        elif kind == "object" and set(node) == {"kind", "entries"} and isinstance(node["entries"], dict):
            value = {name: self.decode(child) for name, child in node["entries"].items()}
        elif kind == "prefix":
            reversed_values = []
            cursor = reference
            while cursor is not None:
                current = self._node(cursor["node"])
                if set(current) != {"kind", "parent", "value"} or current["kind"] != "prefix":
                    raise ValueError("source-mismatch: assembly prefix 结构非法")
                reversed_values.append(self.decode(current["value"]))
                cursor = current["parent"]
            value = list(reversed(reversed_values))
        elif kind == "canonical" and set(node) == {"kind", "item_id", "session_id", "thread_id", "path", "value_hash"}:
            historical_owner = (node["session_id"], node["thread_id"])
            value = self._canonical_item_payload(node["item_id"], historical_owner)
            for part in node["path"]:
                value = value[part]
            if sha256_jcs(value) != node["value_hash"]:
                raise ValueError("source-mismatch: assembly canonical 正文版本不一致")
        else:
            raise ValueError(f"source-mismatch: assembly content kind 非法: {kind!r}")
        self.values[key] = value
        # JSON里的重复值是独立对象；节点共享不能让一次字段编辑改写另一字段。
        return deepcopy(value)


def store_snapshot(connection: sqlite3.Connection, snapshot: dict[str, object]) -> str:
    """仅在封存/显式迁移的原事务内发布节点及有序引用，不维护第二份完整请求。"""
    if not connection.in_transaction:
        raise RuntimeError("assembly content 写入必须位于封存事务内")
    store = _AssemblyContentStore(connection, snapshot["session_id"], snapshot["thread_id"])
    store.capture_sources(snapshot)
    manifest = {**snapshot, "_storage_format": _FORMAT}
    for field in _FIELDS:
        manifest[field] = store.encode(snapshot[field])
    return canonical_json_bytes(manifest).decode("utf-8")


def _manifest(raw: str) -> dict[str, object]:
    manifest = json.loads(raw)
    if canonical_json_bytes(manifest).decode("utf-8") != raw:
        raise ValueError("source-mismatch: assembly content manifest 非canonical JSON")
    if manifest.pop("_storage_format", None) != _FORMAT:
        raise ValueError("source-mismatch: assembly content manifest 格式不受支持")
    return manifest


def snapshot_node_references(
    connection: sqlite3.Connection, raw: str, *, owner: tuple[str, str],
) -> set[tuple[str, str, str]]:
    """只遍历冻结DAG；fork的header证据不承诺能展开未捕获的canonical正文。"""
    manifest = _manifest(raw)
    if (manifest["session_id"], manifest["thread_id"]) != owner:
        raise ValueError("source-mismatch: assembly content manifest owner不一致")
    store = _AssemblyContentStore(connection, manifest["session_id"], manifest["thread_id"])
    pending = [manifest[field] for field in _FIELDS]
    retained: set[tuple[str, str, str]] = set()
    while pending:
        reference = pending.pop()
        if not isinstance(reference, dict):
            raise TypeError("source-mismatch: assembly content 引用非法")
        if set(reference) == {"literal"}:
            continue
        if set(reference) != {"node"} or not isinstance(reference["node"], str):
            raise ValueError("source-mismatch: assembly content 引用非法")
        identity = (*store.assembly_owner, reference["node"])
        if identity in retained:
            continue
        node = store._node(reference["node"])
        kind = node.get("kind")
        if kind == "object" and set(node) == {"kind", "entries"} and isinstance(node["entries"], dict):
            pending.extend(node["entries"].values())
        elif kind == "prefix" and set(node) == {"kind", "parent", "value"}:
            pending.append(node["value"])
            if node["parent"] is not None:
                pending.append(node["parent"])
        elif kind == "canonical" and set(node) == {"kind", "item_id", "session_id", "thread_id", "path", "value_hash"}:
            require_historical_scope(connection, node["session_id"], node["thread_id"])
        elif kind != "value" or set(node) != {"kind", "value"}:
            raise ValueError(f"source-mismatch: assembly content kind 非法: {kind!r}")
        retained.add(identity)
    return retained


def load_snapshot(
    connection: sqlite3.Connection, raw: str, *, canonical_item_ids: set[str] | None = None,
) -> dict[str, object]:
    """schema22仅接受引用manifest；重建依赖历史内容，不访问当前文件或配置。"""
    manifest = _manifest(raw)
    store = _AssemblyContentStore(connection, manifest["session_id"], manifest["thread_id"])
    for field in _FIELDS:
        manifest[field] = store.decode(manifest[field])
    if canonical_item_ids is not None:
        canonical_item_ids.update(store.dependencies)
    return manifest
