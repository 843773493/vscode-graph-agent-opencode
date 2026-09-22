"""per-session session-control.sqlite 的 thread owner binding 字段槽 owner。

本模块承载单 thread owner 侧事实记录这一条垂直链路的唯一实现：

- thread_owner_bindings 行投影与 canonical JSON 列表槽解析；
- 引用槽/条目槽的 2.1 负面合同校验（拒绝物理路径、相对路径片段、NUL 与
  凭据形态键名）；
- create-or-get（ensure）、按 thread_id 读取（get）与 typed 字段槽更新
  （update，含 revision CAS 与 append）；
- 该表行插入的唯一 SQL 实现（发布事务与 ensure 共用，避免第二份行形状）。

ThreadOwnerBindingMixin 由 app.core.session_control_store.SessionControlStore
继承装配；本模块只依赖宿主类提供的 database_path、_connection、_ensure_open()
与 _write_transaction()，不感知其余控制库职责。错误分类沿用
session_control_store 约定：KeyError 目标行缺失、RuntimeError 库被外部改动或
CAS 冲突、ValueError 输入形态非法、TypeError 输入类型错误。


外部篡改边界（D7 取证结论，勿随手加固）：本表行的防线分三层——

1. 结构层：``prefix_epoch``/``revision`` 的 NOT NULL 与 CHECK、
   ``prefix_epoch_reason`` 的 CHECK 由 SQLite 约束兜底，非法直改在写入时即被
   拒绝（IntegrityError）。
2. 语义层：canonical JSON 列表槽（``*_refs``/``tracking_registrations``/
   ``selection_provenance``）与 ``mutation_provenance`` 在读取时解析，损坏或
   结构不符 fail closed（RuntimeError）。
3. 未设读时校验的标量槽：``stable_prefix_hash`` 形态、
   ``stable_prefix_length`` 与 hash 的成对性、``revision`` 单调性、
   ``final_relative_locator`` 的 main/child 语义——这些只在写入路径校验，
   直改后读回不报错。这是有意为之：它们只是 owner 侧记录槽，权威解释属对应
   domain owner；当前唯一生产读者（rollout saver）只取
   ``toolset_compatibility_key``，与期望值不等时自然触发一次新 intent 自愈，
   不存在会被静默利用的路径。按「严禁过度防御」不在此处补读时校验；若将来
   出现依赖这些标量做安全裁决的读者，应在那个读者侧按需校验，而不是在本表
   无条件加固。
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime

from app.core.session_catalog_store import validate_thread_id
from app.core.session_control_primitives import SHA256_HEX_PATTERN
from app.core.session_control_thread_catalog.thread_catalog import (
    validate_thread_relative_locator,
)

__all__ = [
    "THREAD_OWNER_BINDINGS_TABLE_DDL",
    "ThreadOwnerBinding",
    "ThreadOwnerBindingMixin",
]


# thread owner binding 字段槽（2.1，B1/B3）：单 thread 的 owner 侧事实
# 记录（locator/provenance/attachment/variant/resource/activation/
# tracking/prefix epoch/assembly parent/stable prefix/ToolSet revision/
# compatibility key/selection provenance）。引用槽按 2.1 负面合同校验：
# 不收物理路径、相对路径片段与凭据形态字段；列表槽存 canonical JSON。
# prefix epoch 与 ToolSet revision 在此只是 owner 侧记录槽，权威解释仍
# 属 ContextStore/ToolSet domain owner（不构成第二 writer）。
THREAD_OWNER_BINDINGS_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS thread_owner_bindings (
    thread_id TEXT PRIMARY KEY,
    final_relative_locator TEXT NOT NULL DEFAULT '',
    prefix_epoch INTEGER NOT NULL DEFAULT 1 CHECK (prefix_epoch >= 1),
    prefix_epoch_reason TEXT NOT NULL DEFAULT 'initial' CHECK (
        prefix_epoch_reason IN ('initial', 'compaction', 'rewind',
        'toolset_changed')),
    stable_prefix_hash TEXT,
    stable_prefix_length INTEGER,
    desired_toolset_revision INTEGER,
    applied_toolset_revision INTEGER,
    toolset_compatibility_key TEXT,
    policy_compatibility_key TEXT,
    resource_activation_snapshot_ref TEXT,
    assembly_parent_ref TEXT,
    mutation_provenance TEXT,
    attachment_refs TEXT NOT NULL DEFAULT '[]',
    variant_refs TEXT NOT NULL DEFAULT '[]',
    resource_refs TEXT NOT NULL DEFAULT '[]',
    source_revision_refs TEXT NOT NULL DEFAULT '[]',
    tracking_registrations TEXT NOT NULL DEFAULT '[]',
    selection_provenance TEXT NOT NULL DEFAULT '[]',
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
)
"""


_OWNER_BINDING_EPOCH_REASONS = (
    "initial",
    "compaction",
    "rewind",
    "toolset_changed",
)

# 2.1 负面合同：引用/条目槽不得携带凭据形态字段（键名匹配即拒绝）。
_CREDENTIAL_KEY_PATTERN = re.compile(
    r"password|secret|token|api_key|credential",
    re.IGNORECASE,
)

@dataclass(frozen=True, slots=True)
class ThreadOwnerBinding:
    """thread_owner_bindings 行投影（2.1 owner 字段槽，单写者=当前 owner）。

    列表槽以 canonical JSON 落盘、以 dict 元组投影；标量引用槽经
    _validate_ref_text 校验（拒绝物理路径/相对路径片段/NUL）；凭据形态
    键名一律拒绝落盘。prefix epoch 与 ToolSet revision 是 owner 侧记录
    槽，权威解释仍属对应 domain owner。
    """

    thread_id: str
    final_relative_locator: str
    prefix_epoch: int
    prefix_epoch_reason: str
    stable_prefix_hash: str | None
    stable_prefix_length: int | None
    desired_toolset_revision: int | None
    applied_toolset_revision: int | None
    toolset_compatibility_key: str | None
    policy_compatibility_key: str | None
    resource_activation_snapshot_ref: str | None
    assembly_parent_ref: str | None
    mutation_provenance: dict[str, object] | None
    attachment_refs: tuple[dict[str, object], ...]
    variant_refs: tuple[dict[str, object], ...]
    resource_refs: tuple[dict[str, object], ...]
    source_revision_refs: tuple[dict[str, object], ...]
    tracking_registrations: tuple[dict[str, object], ...]
    selection_provenance: tuple[dict[str, object], ...]
    revision: int
    created_at: str
    updated_at: str

def _validate_ref_text(value: object, *, field: str) -> str:
    """引用槽负面校验：非空字符串、无 NUL、非绝对路径、无相对路径片段。"""
    if not isinstance(value, str) or not value:
        raise ValueError(
            f"owner binding {field} 必须是非空字符串: {value!r}"
        )
    if "\x00" in value:
        raise ValueError(f"owner binding {field} 含 NUL 字符: {value!r}")
    if value.startswith("/"):
        raise ValueError(
            f"owner binding {field} 不得是绝对路径形态: {value!r}"
        )
    if "../" in value or value == "..":
        raise ValueError(
            f"owner binding {field} 不得携带相对路径片段: {value!r}"
        )
    return value

def _validate_json_entry(entry: object, *, field: str) -> dict[str, object]:
    """列表槽条目校验：object、字符串键、标量值、键名不含凭据形态。"""
    if not isinstance(entry, dict):
        raise TypeError(
            f"owner binding {field} 条目必须是 object: {entry!r}"
        )
    for key, item in entry.items():
        if not isinstance(key, str) or not key:
            raise ValueError(
                f"owner binding {field} 条目键必须是非空字符串: {key!r}"
            )
        if _CREDENTIAL_KEY_PATTERN.search(key) is not None:
            raise ValueError(
                f"owner binding {field} 条目键疑似凭据形态，拒绝落盘: "
                f"{key!r}"
            )
        if isinstance(item, str):
            _validate_ref_text(item, field=f"{field}.{key}")
        elif isinstance(item, bool | int) or item is None:
            pass
        else:
            raise TypeError(
                f"owner binding {field} 条目值必须是标量: {key!r}={item!r}"
            )
    return entry

def _canonical_json_list(text: str, *, field: str) -> tuple[dict[str, object], ...]:
    """解析列表槽 canonical JSON；结构不符 fail closed（外部改动）。"""
    try:
        parsed = json.loads(text)
    except ValueError as error:
        raise RuntimeError(
            f"owner binding {field} JSON 损坏（fail closed）: {error}"
        ) from error
    if not isinstance(parsed, list):
        raise RuntimeError(  # noqa: TRY004 —— 库内数据损坏属运行时错误
            f"owner binding {field} 必须是 JSON 数组（fail closed）"
        )
    return tuple(_validate_json_entry(entry, field=field) for entry in parsed)

def _owner_binding_from_row(row: sqlite3.Row) -> ThreadOwnerBinding:
    """thread_owner_bindings 行 → 不可变投影（JSON 损坏 fail closed）。"""
    provenance_raw = row["mutation_provenance"]
    provenance: dict[str, object] | None = None
    if provenance_raw is not None:
        try:
            parsed = json.loads(str(provenance_raw))
        except ValueError as error:
            raise RuntimeError(
                "owner binding mutation_provenance JSON 损坏（fail closed）: "
                f"{error}"
            ) from error
        if not isinstance(parsed, dict):
            raise RuntimeError(
                "owner binding mutation_provenance 必须是 JSON object"
            )
        provenance = parsed
    stable_hash = row["stable_prefix_hash"]
    return ThreadOwnerBinding(
        thread_id=str(row["thread_id"]),
        final_relative_locator=str(row["final_relative_locator"]),
        prefix_epoch=int(row["prefix_epoch"]),
        prefix_epoch_reason=str(row["prefix_epoch_reason"]),
        stable_prefix_hash=None if stable_hash is None else str(stable_hash),
        stable_prefix_length=(
            None if row["stable_prefix_length"] is None
            else int(row["stable_prefix_length"])
        ),
        desired_toolset_revision=(
            None if row["desired_toolset_revision"] is None
            else int(row["desired_toolset_revision"])
        ),
        applied_toolset_revision=(
            None if row["applied_toolset_revision"] is None
            else int(row["applied_toolset_revision"])
        ),
        toolset_compatibility_key=(
            None if row["toolset_compatibility_key"] is None
            else str(row["toolset_compatibility_key"])
        ),
        policy_compatibility_key=(
            None if row["policy_compatibility_key"] is None
            else str(row["policy_compatibility_key"])
        ),
        resource_activation_snapshot_ref=(
            None if row["resource_activation_snapshot_ref"] is None
            else str(row["resource_activation_snapshot_ref"])
        ),
        assembly_parent_ref=(
            None if row["assembly_parent_ref"] is None
            else str(row["assembly_parent_ref"])
        ),
        mutation_provenance=provenance,
        attachment_refs=_canonical_json_list(
            str(row["attachment_refs"]), field="attachment_refs"
        ),
        variant_refs=_canonical_json_list(
            str(row["variant_refs"]), field="variant_refs"
        ),
        resource_refs=_canonical_json_list(
            str(row["resource_refs"]), field="resource_refs"
        ),
        source_revision_refs=_canonical_json_list(
            str(row["source_revision_refs"]), field="source_revision_refs"
        ),
        tracking_registrations=_canonical_json_list(
            str(row["tracking_registrations"]),
            field="tracking_registrations",
        ),
        selection_provenance=_canonical_json_list(
            str(row["selection_provenance"]),
            field="selection_provenance",
        ),
        revision=int(row["revision"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


class ThreadOwnerBindingMixin:
    """SessionControlStore 的 thread owner binding 字段槽方法族。

    依赖宿主类提供 database_path、_connection、_ensure_open() 与
    _write_transaction()。
    """

    @staticmethod
    def _insert_thread_owner_binding_row(
        connection: sqlite3.Connection,
        *,
        thread_id: str,
        final_relative_locator: str,
        created_at: str,
        updated_at: str,
    ) -> None:
        """插入初始 owner binding 行的唯一实现（caller 已开写事务）。

        prefix epoch 冻结为 1/initial、六个列表槽为空数组、revision=1；
        发布事务与 ensure_thread_owner_binding 共用本实现，保证行形状
        只有一份定义。
        """
        connection.execute(
            "INSERT INTO thread_owner_bindings (thread_id, "
            "final_relative_locator, prefix_epoch, prefix_epoch_reason, "
            "attachment_refs, variant_refs, resource_refs, "
            "source_revision_refs, tracking_registrations, "
            "selection_provenance, revision, created_at, updated_at) "
            "VALUES (?, ?, 1, 'initial', '[]', '[]', '[]', '[]', '[]', "
            "'[]', 1, ?, ?)",
            (thread_id, final_relative_locator, created_at, updated_at),
        )
    def ensure_thread_owner_binding(
        self, *, thread_id: str, final_relative_locator: str = ""
    ) -> ThreadOwnerBinding:
        """create-or-get thread owner binding 行（发布事务同口径调用）。

        final_relative_locator 为空串仅允许 main thread（内容位于
        session 根目录）；child 必须通过完整 thread relative locator
        验证。已存在时 locator 必须一致（漂移 fail closed）。
        """
        validate_thread_id(thread_id)
        if final_relative_locator:
            validate_thread_relative_locator(final_relative_locator)
        now_text = datetime.now(UTC).isoformat()
        with self._write_transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM thread_owner_bindings WHERE thread_id = ?",
                (thread_id,),
            ).fetchone()
            if existing is not None:
                frozen = str(existing["final_relative_locator"])
                if frozen != final_relative_locator:
                    raise RuntimeError(
                        "thread owner binding locator 不一致（fail closed）: "
                        f"thread_id={thread_id!r}, frozen={frozen!r}, "
                        f"provided={final_relative_locator!r}"
                    )
                return _owner_binding_from_row(existing)
            self._insert_thread_owner_binding_row(
                connection,
                thread_id=thread_id,
                final_relative_locator=final_relative_locator,
                created_at=now_text,
                updated_at=now_text,
            )
            row = connection.execute(
                "SELECT * FROM thread_owner_bindings WHERE thread_id = ?",
                (thread_id,),
            ).fetchone()
            return _owner_binding_from_row(row)

    def get_thread_owner_binding(self, thread_id: str) -> ThreadOwnerBinding:
        """按 thread_id 读取 owner binding；缺失抛 KeyError。"""
        validate_thread_id(thread_id)
        self._ensure_open()
        row = self._connection.execute(
            "SELECT * FROM thread_owner_bindings WHERE thread_id = ?",
            (thread_id,),
        ).fetchone()
        if row is None:
            raise KeyError(
                f"thread owner binding 不存在: thread_id={thread_id!r}, "
                f"path={self.database_path}"
            )
        return _owner_binding_from_row(row)

    def update_thread_owner_binding(
        self,
        thread_id: str,
        *,
        expected_revision: int | None = None,
        prefix_epoch: int | None = None,
        prefix_epoch_reason: str | None = None,
        stable_prefix_hash: str | None = None,
        stable_prefix_length: int | None = None,
        desired_toolset_revision: int | None = None,
        applied_toolset_revision: int | None = None,
        toolset_compatibility_key: str | None = None,
        policy_compatibility_key: str | None = None,
        resource_activation_snapshot_ref: str | None = None,
        assembly_parent_ref: str | None = None,
        mutation_provenance: dict[str, object] | None = None,
        append_attachment_ref: dict[str, object] | None = None,
        append_variant_ref: dict[str, object] | None = None,
        append_resource_ref: dict[str, object] | None = None,
        append_source_revision_ref: dict[str, object] | None = None,
        append_tracking_registration: dict[str, object] | None = None,
        append_selection_provenance: dict[str, object] | None = None,
    ) -> ThreadOwnerBinding:
        """typed 更新 owner binding 字段槽（单写者=当前 generation owner）。

        epoch 与 reason、stable prefix hash 与 length 必须成对提供；
        append_* 每次追加一个经校验的条目；每次更新 revision + 1。
        expected_revision 提供时做 CAS（漂移 fail loud）。权威解释仍属
        对应 domain owner，本表只记录 owner 侧事实。
        """
        validate_thread_id(thread_id)
        updates: dict[str, object] = {}
        if (prefix_epoch is None) != (prefix_epoch_reason is None):
            raise ValueError(
                "prefix_epoch 与 prefix_epoch_reason 必须成对提供"
            )
        if prefix_epoch is not None:
            if not isinstance(prefix_epoch, int) or prefix_epoch < 1:
                raise ValueError(f"prefix_epoch 必须是 >=1 整数: {prefix_epoch!r}")
            if prefix_epoch_reason not in _OWNER_BINDING_EPOCH_REASONS:
                raise ValueError(
                    f"prefix_epoch_reason 非法: {prefix_epoch_reason!r}"
                )
            updates["prefix_epoch"] = prefix_epoch
            updates["prefix_epoch_reason"] = prefix_epoch_reason
        if (stable_prefix_hash is None) != (stable_prefix_length is None):
            raise ValueError(
                "stable_prefix_hash 与 stable_prefix_length 必须成对提供"
            )
        if stable_prefix_hash is not None:
            if SHA256_HEX_PATTERN.fullmatch(stable_prefix_hash) is None:
                raise ValueError(
                    f"stable_prefix_hash 必须是 sha256 小写 hex: "
                    f"{stable_prefix_hash!r}"
                )
            if (
                not isinstance(stable_prefix_length, int)
                or stable_prefix_length < 0
            ):
                raise ValueError(
                    f"stable_prefix_length 必须是 >=0 整数: "
                    f"{stable_prefix_length!r}"
                )
            updates["stable_prefix_hash"] = stable_prefix_hash
            updates["stable_prefix_length"] = stable_prefix_length
        for field, value in (
            ("desired_toolset_revision", desired_toolset_revision),
            ("applied_toolset_revision", applied_toolset_revision),
        ):
            if value is not None:
                if not isinstance(value, int) or value < 1:
                    raise ValueError(f"{field} 必须是 >=1 整数: {value!r}")
                updates[field] = value
        for field, value in (
            ("toolset_compatibility_key", toolset_compatibility_key),
            ("policy_compatibility_key", policy_compatibility_key),
            ("resource_activation_snapshot_ref",
             resource_activation_snapshot_ref),
            ("assembly_parent_ref", assembly_parent_ref),
        ):
            if value is not None:
                updates[field] = _validate_ref_text(value, field=field)
        if mutation_provenance is not None:
            entry = _validate_json_entry(
                mutation_provenance, field="mutation_provenance"
            )
            if set(entry) != {"actor_kind", "actor_ref", "mutated_at"}:
                raise ValueError(
                    "mutation_provenance 键闭集为 "
                    "{actor_kind, actor_ref, mutated_at}: "
                    f"{sorted(entry)!r}"
                )
            datetime.fromisoformat(str(entry["mutated_at"]))
            updates["mutation_provenance"] = json.dumps(
                entry, sort_keys=True, separators=(",", ":"),
                ensure_ascii=False,
            )
        append_columns = (
            ("append_attachment_ref", "attachment_refs"),
            ("append_variant_ref", "variant_refs"),
            ("append_resource_ref", "resource_refs"),
            ("append_source_revision_ref", "source_revision_refs"),
            ("append_tracking_registration", "tracking_registrations"),
            ("append_selection_provenance", "selection_provenance"),
        )
        appends: list[tuple[str, dict[str, object]]] = []
        for param, column in append_columns:
            entry = locals()[param]
            if entry is not None:
                appends.append((
                    column,
                    _validate_json_entry(entry, field=column),
                ))
        if not updates and not appends:
            raise ValueError("update_thread_owner_binding 需要至少一个字段")
        now_text = datetime.now(UTC).isoformat()
        with self._write_transaction() as connection:
            row = connection.execute(
                "SELECT * FROM thread_owner_bindings WHERE thread_id = ?",
                (thread_id,),
            ).fetchone()
            if row is None:
                raise KeyError(
                    f"thread owner binding 不存在: thread_id={thread_id!r}"
                )
            current_revision = int(row["revision"])
            if expected_revision is not None and (
                expected_revision != current_revision
            ):
                raise RuntimeError(
                    "owner binding revision CAS 失败: "
                    f"thread_id={thread_id!r}, "
                    f"expected={expected_revision}, "
                    f"actual={current_revision}"
                )
            set_parts: list[str] = []
            params: list[object] = []
            for field, value in updates.items():
                set_parts.append(f"{field} = ?")
                params.append(value)
            for column, entry in appends:
                current_list = json.loads(str(row[column]))
                if not isinstance(current_list, list):
                    raise RuntimeError(  # noqa: TRY004 —— 库内数据损坏属运行时错误
                        f"owner binding {column} 必须是 JSON 数组"
                    )
                current_list.append(entry)
                set_parts.append(f"{column} = ?")
                params.append(
                    json.dumps(
                        current_list, sort_keys=True, separators=(",", ":"),
                        ensure_ascii=False,
                    )
                )
            set_parts.append("revision = revision + 1")
            set_parts.append("updated_at = ?")
            params.append(now_text)
            params.append(thread_id)
            connection.execute(
                "UPDATE thread_owner_bindings SET "
                + ", ".join(set_parts)
                + " WHERE thread_id = ?",
                params,
            )
            updated = connection.execute(
                "SELECT * FROM thread_owner_bindings WHERE thread_id = ?",
                (thread_id,),
            ).fetchone()
            return _owner_binding_from_row(updated)
