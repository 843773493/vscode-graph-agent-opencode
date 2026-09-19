"""ThreadCreationService —— ThreadCreationRecord child thread 创建流编排
（OpenSpec add-itemized-rollout-context 任务 8.5-A，R20）。

本模块实现 design.md §9（约 804 行）定义的单 child thread 创建协议：

1. **gate exclusive 内冻结 record（含 artifact manifest）**：在建立任何
   staging 前，``session-control.sqlite`` 事务内 create-or-get 不进入
   thread catalog、承担 operation lease 的 ``ThreadCreationRecord
   (state=preparing)``——一次冻结 creation/delegation identity、request
   preimage hash、canonical child ID、最终 relative locator、内部 staging
   locator、GraphBinding/capability/seed/reference、预期 artifact 内容
   清单与 hash、owner Session lifecycle generation 与 catalog/collaboration
   precondition revision；随后在同一 gate 临界区内把重算出的预期内容清单
   冻结进 record。同 key 不同 preimage 立即冲突。
2. **staging 准备（gate 外）**：在 owner session 目录内的不可见区
   ``<session_dir>/.staging/<key>/`` 完整准备 child node（``thread.json``
   manifest、GraphBinding/capability/seed/reference、初始 ContextStore
   占位、caller artifact 文件、``artifact-manifest.json`` 内容清单）+
   durability barrier（文件与目录 fsync），并把落盘结果的实际内容清单
   hash 与 record 冻结值对账。
3. **原子 rename**：staging → 冻结的最终 locator
   ``threads/YYYY/MM/DD/{thread_id}``（日期 = child UTC created_at；父目录
   链 mkdir + 逐级 fsync）。目标已存在且内容一致 → 视为 rename 已完成
   （rename 后/publish 前崩溃的恢复窗口）幂等继续；不一致 → fail closed。
4. **gate exclusive 内 CAS publish（唯一可见性提交点）**：
   ``publish_thread_creation_record`` 在单一 session-control 事务内 CAS
   验证 owner fence 仍为捕获的 active generation、catalog/collaboration
   precondition revision 未漂移后插入 thread_catalog child row 并把
   record 推进为 ``published``。CAS 失败**不发布、不重基**：按 record
   定点清理 staging/final 目录 → ``abort_thread_creation_record(reason)``
   → 抛 RuntimeError（调用方以新 operation 重试）。
5. **admission intent**：把初始 execution 的持久 admission intent 写入
   control store（``create_or_get_initial_execution_intent``，publish 后
   幂等写入）——本轮**不绑定真实 Job**（state 恒 ``pending``），该接口
   契约供 8.5 的幂等 worker 消费。
6. 返回 :class:`ThreadCreationResult`（child_thread_id、final locator、
   record state、admission intent）。

恢复语义（同 key 重入按 record 状态分支，全部定点、禁止扫盘）：

- record 提交后/staging 前：staging 不存在 → 重准备；
- staging 后/rename 前：staging 逐文件校验复用 → 继续 rename；
- rename 后/publish 前：最终 locator 已存在且内容一致 → 直接 publish；
- publish 后/terminal 响应前：record published → 复验最终目录与
  thread_catalog child row 后幂等返回（含 admission intent 补写）；
- staging 准备**中途**崩溃留下不完整 staging 时重入 fail closed（见
  「准备中断窗口」，对齐 R13 模式——无法与外部篡改区分，保留现场供
  人工核账）；
- 无 record 的目录不吸收：staging/final 只按预存 record 校验/清理，
  外来目录内容与预期清单不一致一律 fail closed，不扫描、不猜测、不
  吸收（对齐 app/core/AGENTS.md「不得扫描磁盘并静默吸收改动」）。

红线（模块边界，违反即失去本轮资格）：

- **不切权威、不装配**：本模块不接入 container/main.py，不修改
  resolver/catalog/创建/删除流既有模块；thread 创建机器以 R13
  ``SessionCreationService`` 为结构模板，供 8.5 装配。
- **thread_catalog child row 插入是唯一可见性提交点**：record 状态变化
  本身不构成可见性；正常 reader 只按 catalog 定位、不扫盘，staging 与
  未发布 final 目录不可见。
- **不做 delegate/subagent 业务改造、不做 Job 绑定实现、不做 board
  migration batch**（board migration 由 8.11 的 BoardMigrationRecord
  承担，明确不走本 worker）。
- **双模式无关性**：本流只依赖 workspace catalog store（R10）解析 owner
  session 目录与校验 active，不依赖 resolver 的模式切换（catalog/legacy
  双模式下行为一致——只消费 ``get_node`` 的稳定投影，见报告 §9）。

错误分类约定（沿用 ``session_catalog_store.py``）：``TypeError`` 类型错、
``ValueError`` 形态非法、``KeyError`` 目标不存在、``RuntimeError`` 语义
冲突/外部改动 fail closed。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from app.core.session_catalog_store import (
    SessionCatalogNode,
    SessionCatalogStore,
)
from app.core.session_control_store import (
    SessionControlStore,
    ThreadCreationRecord,
    validate_thread_id,
)
from app.core.session_lifecycle_gate import (
    NavigationTopologyGate,
    SessionDeletionPendingError,
    SessionLifecycleGate,
)

__all__ = [
    "ThreadCreationResult",
    "ThreadCreationService",
    "build_thread_node_files",
    "build_thread_node_inventory",
    "compute_artifact_manifest_hash",
    "compute_thread_creation_preimage_hash",
    "expected_node_directory_names",
    "serialize_artifact_manifest_bytes",
    "validate_artifact_manifest",
    "validate_thread_session_metadata",
    "verify_node_directory",
]

# owner session 目录内的不可见 staging 区（8.5-A internal staging locator
# 的物理形态）：<session_dir>/.staging/<key>/。与 R12 workspace 级
# sessions/.staging、R13 同名约定无冲突——本流的 staging 只存在于 owner
# session 目录内部，且只触碰 record 冻结的 <key> 子目录。
_STAGING_DIR_NAME = ".staging"

# 控制库文件名（与 R12/R13/R14 一致）。
_CONTROL_DATABASE_NAME = "session-control.sqlite"

# child node 结构文件（确定性内容；清单只覆盖文件，目录集单独断言）。
_THREAD_MANIFEST_NAME = "thread.json"
_ARTIFACT_MANIFEST_NAME = "artifact-manifest.json"
_CONTEXT_STORE_RELATIVE_PATH = "rollout/context-store.json"
_ARTIFACTS_DIR_NAME = "artifacts"

# child node 必备目录集（runs/ 为初始 execution runs 占位空目录）。
_EXPECTED_NODE_DIRS = ("artifacts", "rollout", "runs")

# thread.json 调用方侧字段闭集（session_metadata 的形态；R13 六字段闭集
# 的 thread 版——GraphBinding/capability/seed/reference 必须显式提供，
# 缺失不得推断）。TODO(8.5): 字段集在 thread manifest 正式 schema 落地时
# 与 8.4 ThreadRuntimeBinding 对齐复核。
_METADATA_CALLER_KEYS = frozenset(
    {
        "graph_binding",
        "capability_profile",
        "task_seed",
        "task_reference",
    }
)

# GraphBinding 四元组（design.md §9「至少保存」四字段；本轮收严为恰四键，
# 额外字段随 8.4 ThreadRuntimeBinding 落地再放行）。
_GRAPH_BINDING_KEYS = frozenset(
    {
        "graph_id",
        "graph_revision",
        "graph_schema_hash",
        "capability_profile_hash",
    }
)

_INITIAL_STATE_VALUES = ("running", "idle")


def canonical_json_text(payload: object) -> str:
    """canonical JSON 文本（紧凑 + sort_keys，preimage/冻结列共用口径）。

    与 R13 的 preimage 序列化同型：``sort_keys=True`` + 紧凑分隔符 +
    UTF-8（不追求跨实现 JCS 兼容，保证同 preimage 判定跨重入稳定）。
    """
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def canonical_json_bytes(payload: object) -> bytes:
    """canonical JSON 文件字节（indent 形式，thread.json/清单文件共用）。"""
    return (
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2)
        + "\n"
    ).encode("utf-8")


def validate_thread_session_metadata(
    initial_state: str,
    session_metadata: dict[str, object],
    *,
    compute_capability_profile_hash: Callable[..., str],
) -> None:
    """校验调用方 session_metadata 四字段闭集与 seed/初始 state 一致性。

    - 字段集恰为 ``_METADATA_CALLER_KEYS``（缺/多一律 fail closed——
      「无 seed 必须显式 idle 且进 preimage」，缺失不得推断）；
    - ``graph_binding`` 恰含 GraphBinding 四元组：``graph_revision`` 必须
      是不为 bool 的整数且 >= 1（对齐真实 GraphBinding 的 int 口径；
      str revision 在 GraphBinding 构造时必然失败，不保留字符串兼容
      双轨），其余三字段必须是非空字符串；
    - ``capability_profile`` 经注入的 ``compute_capability_profile_hash``
      按平面标量映射口径校验并计算摘要（真实实现位于
      app/agents/graph_binding；core 层禁止反向 import agents，故由
      装配侧注入同一实现，禁止复制第二份 hash 逻辑），
      ``graph_binding['capability_profile_hash']`` 必须与该计算结果
      一致（防自述字段漂移）；
    - ``task_seed`` / ``task_reference`` 必须是 dict 或 None；
    - ``initial_state='idle'`` → ``task_seed`` 必须为 None（idle 不得携带
      seed）；``initial_state='running'`` → ``task_seed`` 必须为非 None
      dict（running 必须有明确 task seed）。
    """
    if not isinstance(session_metadata, dict):
        raise TypeError(
            f"session_metadata 必须是 dict: {type(session_metadata).__name__}"
        )
    if initial_state not in _INITIAL_STATE_VALUES:
        raise ValueError(f"initial_state 非法: {initial_state!r}")
    keys = set(session_metadata)
    missing = sorted(_METADATA_CALLER_KEYS - keys)
    unexpected = sorted(keys - _METADATA_CALLER_KEYS)
    if missing or unexpected:
        raise ValueError(
            "session_metadata 字段集非法（thread manifest 调用方字段闭集，"
            "缺失不得推断）: "
            f"missing={missing}, unexpected={unexpected}, "
            f"allowed={sorted(_METADATA_CALLER_KEYS)}"
        )
    graph_binding = session_metadata["graph_binding"]
    if not isinstance(graph_binding, dict) or set(graph_binding) != (
        _GRAPH_BINDING_KEYS
    ):
        raise ValueError(
            "graph_binding 必须恰含 GraphBinding 四元组 "
            f"{sorted(_GRAPH_BINDING_KEYS)}: {graph_binding!r}"
        )
    graph_revision = graph_binding["graph_revision"]
    if not isinstance(graph_revision, int) or isinstance(graph_revision, bool):
        raise TypeError(
            "graph_binding['graph_revision'] 必须是整数（对齐真实 "
            f"GraphBinding，不接受字符串）: {graph_revision!r}"
        )
    if graph_revision < 1:
        raise ValueError(
            "graph_binding['graph_revision'] 必须 >= 1: "
            f"实际 {graph_revision}"
        )
    for key in sorted(_GRAPH_BINDING_KEYS - {"graph_revision"}):
        value = graph_binding[key]
        if not isinstance(value, str) or not value:
            raise ValueError(
                f"graph_binding[{key!r}] 必须是非空字符串: {value!r}"
            )
    profile_hash = compute_capability_profile_hash(
        capability_profile=session_metadata["capability_profile"]
    )
    if graph_binding["capability_profile_hash"] != profile_hash:
        raise ValueError(
            "graph_binding['capability_profile_hash'] 与 capability_profile "
            "内容不一致（自述字段漂移）: "
            f"计算={profile_hash!r}, "
            f"自述={graph_binding['capability_profile_hash']!r}"
        )
    task_seed = session_metadata["task_seed"]
    task_reference = session_metadata["task_reference"]
    if task_seed is not None and not isinstance(task_seed, dict):
        raise ValueError(f"task_seed 必须是 dict 或 None: {task_seed!r}")
    if task_reference is not None and not isinstance(task_reference, dict):
        raise ValueError(
            f"task_reference 必须是 dict 或 None: {task_reference!r}"
        )
    if initial_state == "idle" and task_seed is not None:
        raise ValueError(
            "initial_state='idle' 不得携带 task_seed（无 seed 必须显式 "
            f"idle 且进入 preimage）: task_seed={task_seed!r}"
        )
    if initial_state == "running" and task_seed is None:
        raise ValueError(
            "initial_state='running' 必须携带显式 task_seed（缺失推断被拒）"
        )


def validate_artifact_manifest(artifact_manifest: dict[str, object]) -> None:
    """校验调用方 artifact 载荷：``{相对路径: 文本内容}``。

    相对路径必须是安全多段路径（无绝对路径/``..``/反斜杠/空段/NUL），
    落位 ``<child node>/artifacts/<相对路径>``；内容必须是 str（UTF-8
    文本）。空 dict 合法（无附加 artifact）。
    """
    if not isinstance(artifact_manifest, dict):
        raise TypeError(
            f"artifact_manifest 必须是 dict: {type(artifact_manifest).__name__}"
        )
    for rel_path, content in artifact_manifest.items():
        if not isinstance(rel_path, str) or not rel_path:
            raise ValueError(f"artifact 路径必须是非空字符串: {rel_path!r}")
        if (
            rel_path.startswith("/")
            or rel_path.endswith("/")
            or "\\" in rel_path
            or "\x00" in rel_path
        ):
            raise ValueError(f"artifact 路径形态非法: {rel_path!r}")
        segments = rel_path.split("/")
        if any(segment in ("", ".", "..") for segment in segments):
            raise ValueError(f"artifact 路径含空段/./..: {rel_path!r}")
        if not isinstance(content, str):
            raise TypeError(
                f"artifact 内容必须是 str（UTF-8 文本）: {rel_path!r}"
            )


def compute_thread_creation_preimage_hash(
    *,
    workspace_id: str,
    session_id: str,
    thread_id: str | None,
    delegation_id: str | None,
    initial_state: str,
    session_metadata: dict[str, object],
    artifact_manifest: dict[str, object],
) -> str:
    """preimage_hash = sha256(canonical JSON of 创建请求六元组)。

    delegated child 的 ``delegation_id`` 纳入 preimage（tasks.md 8.5-A）；
    ``thread_id`` 为 None（软件分配）时不进 preimage（对齐 R13：分配结果
    不属于调用方请求 preimage）。
    """
    payload = {
        "workspace_id": workspace_id,
        "session_id": session_id,
        "thread_id": thread_id,
        "delegation_id": delegation_id,
        "initial_state": initial_state,
        "session_metadata": session_metadata,
        "artifact_manifest": artifact_manifest,
    }
    return hashlib.sha256(
        canonical_json_text(payload).encode("utf-8")
    ).hexdigest()


def build_thread_node_files(
    *,
    record: ThreadCreationRecord,
    session_id: str,
    artifact_manifest: dict[str, str],
) -> dict[str, bytes]:
    """构造 child node 全部确定性文件内容（纯函数；不含清单文件本体）。

    内容是 record 冻结值 + 调用方 artifact 载荷的确定性函数——同 key 同
    preimage 重入逐字节稳定，这是「staging 校验复用」「恢复按预存 record
    定点校验」的前提。
    """
    thread_manifest = {
        "thread_id": record.child_thread_id,
        "session_id": session_id,
        "kind": "child",
        "created_at": record.child_created_at,
        "initial_state": record.initial_state,
        "graph_binding": json.loads(record.graph_binding),
        "capability_profile": json.loads(record.capability_profile),
        "task_seed": (
            json.loads(record.task_seed) if record.task_seed is not None else None
        ),
        "task_reference": (
            json.loads(record.task_reference)
            if record.task_reference is not None
            else None
        ),
    }
    context_store = {
        "thread_id": record.child_thread_id,
        "created_at": record.child_created_at,
        # 初始 ContextStore 占位（空 item 集）；真实 ContextStore 落盘格式
        # 归 8.5 接线，当前占位保证结构完整性与清单可校验。
        "items": [],
    }
    files: dict[str, bytes] = {
        _THREAD_MANIFEST_NAME: canonical_json_bytes(thread_manifest),
        _CONTEXT_STORE_RELATIVE_PATH: canonical_json_bytes(context_store),
    }
    for rel_path, content in artifact_manifest.items():
        files[f"{_ARTIFACTS_DIR_NAME}/{rel_path}"] = content.encode("utf-8")
    return files


def build_thread_node_inventory(files: dict[str, bytes]) -> dict[str, str]:
    """内容清单：相对路径 → sha256 小写 hex（键按 canonical 序列化排序）。"""
    return {
        rel_path: hashlib.sha256(content).hexdigest()
        for rel_path, content in sorted(files.items())
    }


def serialize_artifact_manifest_bytes(inventory: dict[str, str]) -> bytes:
    """``artifact-manifest.json`` 文件字节（清单本体的落盘形态）。"""
    return canonical_json_bytes(inventory)


def compute_artifact_manifest_hash(inventory: dict[str, str]) -> str:
    """清单 hash = sha256(canonical JSON 文本)（record 冻结口径）。"""
    return hashlib.sha256(
        canonical_json_text(inventory).encode("utf-8")
    ).hexdigest()


def expected_node_directory_names(
    files: dict[str, bytes],
) -> set[str]:
    """child node 的预期目录集（相对路径，posix 分隔）。

    结构目录（artifacts/rollout/runs）+ caller artifact 嵌套路径的**全部
    中间祖先目录**（``artifacts/a/b/c.txt`` → ``artifacts/a``、
    ``artifacts/a/b``）。
    """
    directories = set(_EXPECTED_NODE_DIRS)
    for key in files:
        if not key.startswith(f"{_ARTIFACTS_DIR_NAME}/"):
            continue
        segments = key.split("/")
        # 逐级累积祖先目录（不含文件名段）。
        for depth in range(1, len(segments)):
            directories.add("/".join(segments[:depth]))
    return directories


def verify_node_directory(
    directory: Path,
    expected_files: dict[str, bytes],
    inventory: dict[str, str],
    *,
    stage: str,
) -> None:
    """逐文件校验 staging/final 目录与预期一致；不一致 fail closed。

    校验面：目录集、文件集（含 symlink 拒绝）、每个文件 sha256、
    ``artifact-manifest.json`` 字节。无 record 对应的目录（外部放置）
    因内容不一致被拒绝——不吸收。
    """
    if not directory.is_dir() or directory.is_symlink():
        raise RuntimeError(
            f"{stage}: 目录缺失或不是目录（外部改动，fail closed）: "
            f"{directory}"
        )
    actual_files: dict[str, bytes] = {}
    actual_dirs: set[str] = set()
    for root, dir_names, file_names in os.walk(directory, followlinks=False):
        root_path = Path(root)
        for dir_name in dir_names:
            dir_entry = root_path / dir_name
            if dir_entry.is_symlink():
                raise RuntimeError(
                    f"{stage}: 发现 symlink 目录（外部改动，fail closed）: "
                    f"{dir_entry}"
                )
            actual_dirs.add(dir_entry.relative_to(directory).as_posix())
        for file_name in file_names:
            file_entry = root_path / file_name
            if file_entry.is_symlink():
                raise RuntimeError(
                    f"{stage}: 发现 symlink 文件（外部改动，fail closed）: "
                    f"{file_entry}"
                )
            relative = file_entry.relative_to(directory).as_posix()
            actual_files[relative] = file_entry.read_bytes()
    expected_dir_set = expected_node_directory_names(expected_files)
    if actual_dirs != expected_dir_set:
        raise RuntimeError(
            f"{stage}: 目录集与预期不一致（外部改动或上次准备中断，fail "
            f"closed）: path={directory}, "
            f"expected={sorted(expected_dir_set)}, "
            f"actual={sorted(actual_dirs)}"
        )
    expected_file_set = set(expected_files) | {_ARTIFACT_MANIFEST_NAME}
    if set(actual_files) != expected_file_set:
        raise RuntimeError(
            f"{stage}: 文件集与预期不一致（外部改动或上次准备中断，fail "
            f"closed）: path={directory}, "
            f"expected={sorted(expected_file_set)}, "
            f"actual={sorted(actual_files)}"
        )
    for rel_path, payload in sorted(expected_files.items()):
        if hashlib.sha256(actual_files[rel_path]).hexdigest() != inventory[
            rel_path
        ]:
            raise RuntimeError(
                f"{stage}: {rel_path} sha256 与预期不一致（外部改动，fail "
                f"closed）: path={directory / rel_path}"
            )
    actual_manifest_bytes = actual_files[_ARTIFACT_MANIFEST_NAME]
    if actual_manifest_bytes != serialize_artifact_manifest_bytes(inventory):
        raise RuntimeError(
            f"{stage}: artifact-manifest.json 字节与预期不一致（外部改动，"
            f"fail closed）: path={directory / _ARTIFACT_MANIFEST_NAME}"
        )


def _fsync_directory(directory: Path) -> None:
    """fsync 目录项，保证新建/改名条目的持久性（模式对齐 R13/R14）。"""
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_file(path: Path) -> None:
    """fsync 已存在文件（durability barrier 组成部分）。"""
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_write_bytes(path: Path, payload: bytes) -> None:
    """tempfile + fsync + os.replace 的原子写（模式对齐 R13）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        dir=path.parent,
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)
    _fsync_directory(path.parent)


@dataclass(frozen=True, slots=True)
class ThreadCreationResult:
    """一次 child thread 创建流的最终结果（record 已 published 的投影）。"""

    child_thread_id: str
    final_relative_locator: str
    record_state: str
    admission_intent: dict[str, object]
    admission_state: str
    execution_binding_id: str
    frozen_job_id: str


class ThreadCreationService:
    """child thread 创建流编排（不切权威、不装配，结构对照 R13）。

    ``create`` 状态机（幂等：同 key 重入按 record 状态分支收敛；并发同
    key 由 per-key ``asyncio.Lock`` 串行；gate 只包住两个 session-control
    短临界区——锁序 gate → 至多一个 SQLite 写事务）：

    1. gate exclusive 内：解析 owner session 节点（active + workspace 一
       致 + main row 指针一致 + control store 绑定校验）→
       ``create_or_get_thread_creation_record`` → preparing 时重算预期内
       容清单并 ``freeze_thread_creation_artifact_manifest`` → 出 gate。
       published → 幂等恢复；aborted → RuntimeError（换新 key 重试）。
    2. preparing：先探最终 locator——已存在且内容一致 → 视为 rename 已
       完成（恢复窗口），直接 publish；staging 已存在 → 逐文件校验复用；
       否则完整准备 staging + durability barrier + 实际清单对账，再原子
       rename。
    3. gate exclusive 内 CAS publish；失败 → 定点清理 record 列出的
       staging/final 目录 → abort（含原因）→ RuntimeError。
    4. publish 后幂等写入初始 execution admission intent（不绑定 Job），
       复验最终目录后返回 result。
    """

    def __init__(
        self,
        *,
        store: SessionCatalogStore,
        control_store: SessionControlStore,
        sessions_root: Path,
        workspace_id: str,
        compute_capability_profile_hash: Callable[..., str],
        gate: NavigationTopologyGate | None = None,
        session_gate: SessionLifecycleGate | None = None,
    ) -> None:
        if not isinstance(store, SessionCatalogStore):
            raise TypeError(f"store 必须是 SessionCatalogStore: {store!r}")
        if not isinstance(control_store, SessionControlStore):
            raise TypeError(
                f"control_store 必须是 SessionControlStore: {control_store!r}"
            )
        if not isinstance(sessions_root, Path):
            raise TypeError(f"sessions_root 必须是 Path: {sessions_root!r}")
        if not isinstance(workspace_id, str) or not workspace_id:
            raise ValueError(f"workspace_id 不能为空: {workspace_id!r}")
        if not callable(compute_capability_profile_hash):
            raise TypeError(
                "compute_capability_profile_hash 必须是可调用对象（接受 "
                "keyword-only capability_profile 并返回摘要字符串；真实实现在 "
                "app/agents/graph_binding，由装配侧注入以避免 core 反向依赖 "
                "agents）: "
                f"{compute_capability_profile_hash!r}"
            )
        self._store = store
        self._control_store = control_store
        self._sessions_root = sessions_root.expanduser().resolve()
        # service 与 catalog store 必须指向同一物理根（owner session 目录
        # 定位与 catalog locator 一致的前提），不一致 fail fast。
        if self._sessions_root != store.sessions_root:
            raise ValueError(
                "sessions_root 与 catalog store 的 sessions_root 不一致: "
                f"service={self._sessions_root}, store={store.sessions_root}"
            )
        self._workspace_id = workspace_id
        self._compute_capability_profile_hash = compute_capability_profile_hash
        # 跨进程 gate 原语（topology 文件锁；per-session 生命周期 gate 由
        # session_lifecycle_gate 承载，B3 准入链路接入）。
        self._gate = (
            gate if gate is not None else NavigationTopologyGate(self._sessions_root)
        )
        # 2.3-A 准入链路的 per-session 生命周期 gate（跨进程文件锁）。
        self._session_gate = (
            session_gate
            if session_gate is not None
            else SessionLifecycleGate(self._sessions_root)
        )
        self._key_locks: dict[str, asyncio.Lock] = {}

    # ------------------------------------------------------------------
    # 公开入口
    # ------------------------------------------------------------------

    async def create(
        self,
        *,
        idempotency_key: str,
        session_id: str,
        thread_id: str | None,
        delegation_id: str | None,
        initial_state: str,
        session_metadata: dict[str, object],
        artifact_manifest: dict[str, object],
        collaboration_member: dict[str, object] | None = None,
    ) -> ThreadCreationResult:
        """执行（或幂等恢复）一次 child thread 创建（协议见类 docstring）。

        ``thread_id=None`` 时 child ID 由 store 软件分配（``thr_``）；
        提供时必须为 canonical thread ID。``delegation_id`` 提供即为
        delegated child（纳入 preimage 与部分唯一约束）。
        """
        self._validate_create_inputs(
            idempotency_key=idempotency_key,
            session_id=session_id,
            thread_id=thread_id,
            delegation_id=delegation_id,
            initial_state=initial_state,
            session_metadata=session_metadata,
            artifact_manifest=artifact_manifest,
            collaboration_member=collaboration_member,
        )
        preimage_hash = compute_thread_creation_preimage_hash(
            workspace_id=self._workspace_id,
            session_id=session_id,
            thread_id=thread_id,
            delegation_id=delegation_id,
            initial_state=initial_state,
            session_metadata=session_metadata,
            artifact_manifest=artifact_manifest,
        )
        # 进程内同 key 串行：并发同 key create 收敛到同一结果。
        async with self._key_lock(idempotency_key):
            # 步骤 1：准入 = topology shared → Session gate exclusive（2.3-A
            # 固定锁序），fresh 验证后冻结 record（等价 operation lease）。
            async with self._gate.shared(), self._session_gate.exclusive(
                session_id
            ):
                node = self._store.get_node(session_id)
                self._validate_owner_node(node)
                session_dir = self._session_dir_for(node)
                self._validate_control_store_binding(session_dir)
                self._control_store.verify_matches_catalog_main_thread(
                    str(node.main_thread_id)
                )
                collaboration_revision: int | None = None
                if collaboration_member is not None:
                    # ledger 登记先于 record 冻结（同一 gate 临界区内）：
                    # record 冻结登记后的 ledger revision，publish 时 CAS
                    # 校验未漂移；重入时登记幂等、revision 不变。
                    collaboration_revision = (
                        self._control_store.register_collaboration_member(
                            delegation_id=delegation_id,  # type: ignore[arg-type]
                            coordinator_session_id=session_id,
                            coordinator_thread_id=str(node.main_thread_id),
                            role=str(collaboration_member["role"]),
                            subagent_type=str(collaboration_member["subagent_type"]),
                            title=str(collaboration_member["title"]),
                            task_seed=canonical_json_text(
                                session_metadata["task_seed"]
                            ),
                        )
                    )
                record = self._control_store.create_or_get_thread_creation_record(
                    idempotency_key=idempotency_key,
                    initial_state=initial_state,
                    preimage_hash=preimage_hash,
                    graph_binding=canonical_json_text(
                        session_metadata["graph_binding"]
                    ),
                    capability_profile=canonical_json_text(
                        session_metadata["capability_profile"]
                    ),
                    created_at=datetime.now(UTC),
                    thread_id=thread_id,
                    delegation_id=delegation_id,
                    collaboration_precondition_revision=collaboration_revision,
                    task_seed=(
                        canonical_json_text(session_metadata["task_seed"])
                        if session_metadata["task_seed"] is not None
                        else None
                    ),
                    task_reference=(
                        canonical_json_text(session_metadata["task_reference"])
                        if session_metadata["task_reference"] is not None
                        else None
                    ),
                )
                if record.state == "preparing":
                    record = self._freeze_artifact_manifest(
                        record,
                        session_id=session_id,
                        artifact_manifest=artifact_manifest,
                    )
            if record.state == "aborted":
                raise RuntimeError(
                    "thread creation record 已中止，调用方须换新 "
                    f"idempotency_key 重试: key={idempotency_key!r}, "
                    f"reason={record.abort_reason!r}"
                )
            # 步骤 2/3：staging 准备 + 原子 rename（gate 外）。
            staging_dir = session_dir / record.staging_locator
            final_dir = session_dir / record.final_relative_locator
            expected_files = build_thread_node_files(
                record=record,
                session_id=session_id,
                artifact_manifest=artifact_manifest,
            )
            inventory = build_thread_node_inventory(expected_files)
            if record.state == "published" and not (
                final_dir.exists() or final_dir.is_symlink()
            ):
                # published 的 record 其 final 目录已被外部删除：**不重建**。
                # rename 先于 publish，协议内崩溃点不可能产生该状态；重建
                # 等于静默吸收外部对可见性产物的改动（对齐会话目录「绕过
                # 软件修改必须明确报错」原则）。
                raise RuntimeError(
                    "thread creation record 已 published 但最终 locator 目录"
                    "缺失（外部删除可见性产物，fail closed，拒绝重建）: "
                    f"key={idempotency_key!r}, final={final_dir}"
                )
            if final_dir.exists() or final_dir.is_symlink():
                # rename 后、publish 前崩溃的恢复窗口：目标内容一致才继续。
                self._verify_node_directory(
                    final_dir,
                    expected_files,
                    inventory,
                    stage="恢复(最终 locator 已存在)",
                )
                if staging_dir.exists() or staging_dir.is_symlink():
                    raise RuntimeError(
                        "最终 locator 与 staging 同时存在（外部改动，fail "
                        f"closed）: key={idempotency_key!r}, "
                        f"final={final_dir}, staging={staging_dir}"
                    )
                return await self._publish_and_finalize(
                    record,
                    session_id=session_id,
                    session_dir=session_dir,
                    final_dir=final_dir,
                    expected_files=expected_files,
                    inventory=inventory,
                )
            self._prepare_staging(
                staging_dir, session_dir, expected_files, inventory
            )
            self._rename_staging_to_final(
                staging_dir,
                final_dir,
                session_dir,
                expected_files,
                inventory,
            )
            # 步骤 4/5：gate exclusive 内 CAS publish + admission intent。
            return await self._publish_and_finalize(
                record,
                session_id=session_id,
                session_dir=session_dir,
                final_dir=final_dir,
                expected_files=expected_files,
                inventory=inventory,
            )

    # ------------------------------------------------------------------
    # 输入校验与并发原语
    # ------------------------------------------------------------------

    def _validate_create_inputs(
        self,
        *,
        idempotency_key: str,
        session_id: str,
        thread_id: str | None,
        delegation_id: str | None,
        initial_state: str,
        session_metadata: dict[str, object],
        artifact_manifest: dict[str, object],
        collaboration_member: dict[str, object] | None = None,
    ) -> None:
        """create 入参校验（在任何状态变更之前 fail fast）。"""
        if not isinstance(idempotency_key, str) or not idempotency_key:
            raise ValueError(
                f"idempotency_key 不能为空: {idempotency_key!r}"
            )
        # idempotency_key 是 session 内 .staging/ 目录名，必须是安全单段
        # 路径名（与 store 侧校验同口径，服务层先行 fail fast）。
        if (
            idempotency_key in (".", "..")
            or "/" in idempotency_key
            or "\\" in idempotency_key
            or "\x00" in idempotency_key
        ):
            raise ValueError(
                "idempotency_key 必须是安全单段路径名（不含分隔符/./..）: "
                f"{idempotency_key!r}"
            )
        if not isinstance(session_id, str) or not session_id:
            raise ValueError(f"session_id 不能为空: {session_id!r}")
        if thread_id is not None:
            validate_thread_id(thread_id)
        if delegation_id is not None and (
            not isinstance(delegation_id, str) or not delegation_id
        ):
            raise ValueError(
                f"delegated child 必须携带非空 delegation_id: {delegation_id!r}"
            )
        validate_thread_session_metadata(
            initial_state,
            session_metadata,
            compute_capability_profile_hash=(
                self._compute_capability_profile_hash
            ),
        )
        validate_artifact_manifest(artifact_manifest)
        # preimage 计算兼作 JSON 可序列化校验（不可序列化值 fail fast）。
        compute_thread_creation_preimage_hash(
            workspace_id=self._workspace_id,
            session_id=session_id,
            thread_id=thread_id,
            delegation_id=delegation_id,
            initial_state=initial_state,
            session_metadata=session_metadata,
            artifact_manifest=artifact_manifest,
        )
        if collaboration_member is None:
            return
        if delegation_id is None:
            raise ValueError(
                "collaboration member 登记必须携带 delegation_id（manual "
                f"creation 不登记）: key={idempotency_key!r}"
            )
        if not isinstance(collaboration_member, dict) or set(
            collaboration_member
        ) != {"role", "subagent_type", "title"}:
            raise ValueError(
                "collaboration_member 必须是 {role, subagent_type, title} "
                f"闭集: {sorted(collaboration_member)!r}"
            )
        for key in ("role", "subagent_type", "title"):
            value = collaboration_member[key]
            if not isinstance(value, str) or not value:
                raise ValueError(
                    f"collaboration_member[{key!r}] 必须是非空字符串: {value!r}"
                )

    def _key_lock(self, idempotency_key: str) -> asyncio.Lock:
        """按 key create-or-get 进程内串行锁（锁随进程生命周期保留）。"""
        lock = self._key_locks.get(idempotency_key)
        if lock is None:
            lock = asyncio.Lock()
            self._key_locks[idempotency_key] = lock
        return lock

    # ------------------------------------------------------------------
    # owner session 定位与绑定校验
    # ------------------------------------------------------------------

    def _validate_owner_node(self, node: SessionCatalogNode) -> None:
        """owner session 节点校验：session 类型、active、同 workspace。"""
        if node.kind != "session":
            raise ValueError(
                f"owner 节点不是 session: node_id={node.node_id!r}, "
                f"kind={node.kind!r}"
            )
        if node.state == "deleting":
            # 2.3-D：catalog deleting 后新准入立即失败（统一错误合同）。
            raise SessionDeletionPendingError(
                "session_deletion_pending: owner session catalog 已进入删除流，"
                f"拒绝新 thread 创建准入: session_id={node.node_id!r}"
            )
        if node.state != "active":
            raise RuntimeError(
                "owner session 在 workspace catalog 非 active（创建/删除"
                f"双顺序红线，fail closed）: session_id={node.node_id!r}, "
                f"state={node.state!r}"
            )
        if node.workspace_id != self._workspace_id:
            raise ValueError(
                "owner session 与 service workspace 不一致: "
                f"session_workspace={node.workspace_id!r}, "
                f"service_workspace={self._workspace_id!r}"
            )
        if node.storage_relative_locator is None:
            raise RuntimeError(
                "owner session 节点缺 storage_relative_locator（catalog 被"
                f"外部改动，fail closed）: session_id={node.node_id!r}"
            )

    def _session_dir_for(self, node: SessionCatalogNode) -> Path:
        locator = str(node.storage_relative_locator)
        return self._sessions_root / locator[len("sessions/"):]

    def _validate_control_store_binding(self, session_dir: Path) -> None:
        """control store 必须绑定 owner session 目录（误绑 fail fast）。"""
        expected_path = (session_dir / _CONTROL_DATABASE_NAME).resolve()
        if self._control_store.database_path != expected_path:
            raise ValueError(
                "control_store 与 owner session 目录不一致（误绑其它 "
                "session 的控制库，fail fast）: "
                f"expected={expected_path}, "
                f"actual={self._control_store.database_path}"
            )

    # ------------------------------------------------------------------
    # artifact 清单冻结（gate 内短事务）
    # ------------------------------------------------------------------

    def _freeze_artifact_manifest(
        self,
        record: ThreadCreationRecord,
        *,
        session_id: str,
        artifact_manifest: dict[str, object],
    ) -> ThreadCreationRecord:
        """重算预期内容清单并冻结进 record（幂等；漂移 fail closed）。

        清单是 record 冻结值 + 调用方 artifact 载荷（preimage 覆盖）的
        确定性函数：恢复重入重算结果与既有冻结值一致 → 幂等；不一致即
        外部改动/确定性漂移 → fail closed。
        """
        expected_files = build_thread_node_files(
            record=record,
            session_id=session_id,
            artifact_manifest=artifact_manifest,
        )
        inventory = build_thread_node_inventory(expected_files)
        return self._control_store.freeze_thread_creation_artifact_manifest(
            record.thread_creation_idempotency_key,
            artifact_manifest=canonical_json_text(inventory),
            artifact_manifest_hash=compute_artifact_manifest_hash(inventory),
        )

    # ------------------------------------------------------------------
    # staging 准备 + durability barrier
    # ------------------------------------------------------------------

    def _prepare_staging(
        self,
        staging_dir: Path,
        session_dir: Path,
        expected_files: dict[str, bytes],
        inventory: dict[str, str],
    ) -> None:
        """staging 已存在 → 逐文件校验复用；不存在 → 完整准备 + barrier。"""
        if staging_dir.exists() or staging_dir.is_symlink():
            self._verify_node_directory(
                staging_dir,
                expected_files,
                inventory,
                stage="staging 复验",
            )
            return
        # 准备中断（任一步骤抛错）：不删除现场（无法与外部篡改区分），
        # 保留不完整 staging 供人工核账，重入 fail closed（「准备中断
        # 窗口」——人工清理 staging 后同 key 重试即可完整重准备）。
        staging_dir.mkdir(parents=True, exist_ok=False)
        for dir_name in _EXPECTED_NODE_DIRS:
            (staging_dir / dir_name).mkdir(exist_ok=False)
        for rel_path, payload in expected_files.items():
            _atomic_write_bytes(staging_dir / rel_path, payload)
        _atomic_write_bytes(
            staging_dir / _ARTIFACT_MANIFEST_NAME,
            serialize_artifact_manifest_bytes(inventory),
        )
        # durability barrier：文件已各自 fsync（原子写）；此处补目录树
        # fsync（新建目录条目的持久性，含新建的 .staging 挂点本身）。
        for dir_name in _EXPECTED_NODE_DIRS:
            _fsync_directory(staging_dir / dir_name)
        _fsync_directory(staging_dir)
        _fsync_directory(staging_dir.parent)
        _fsync_directory(session_dir)
        # 落盘后对账：实际内容清单必须与 record 冻结值一致（fail loud
        # 捕获文件系统异常，不留静默漂移）。
        self._verify_node_directory(
            staging_dir,
            expected_files,
            inventory,
            stage="staging 落盘对账",
        )

    def _verify_node_directory(
        self,
        directory: Path,
        expected_files: dict[str, bytes],
        inventory: dict[str, str],
        *,
        stage: str,
    ) -> None:
        """逐文件校验（委托模块级纯函数 ``verify_node_directory``）。"""
        verify_node_directory(
            directory,
            expected_files,
            inventory,
            stage=stage,
        )

    # ------------------------------------------------------------------
    # rename 与 publish
    # ------------------------------------------------------------------

    def _rename_staging_to_final(
        self,
        staging_dir: Path,
        final_dir: Path,
        session_dir: Path,
        expected_files: dict[str, bytes],
        inventory: dict[str, str],
    ) -> None:
        """staging 原子 rename 到冻结最终 locator（父目录链 mkdir + fsync）。"""
        stage = "rename 到最终 locator"
        if not staging_dir.is_dir() or staging_dir.is_symlink():
            raise RuntimeError(
                f"{stage}: staging 缺失或不是目录: {staging_dir}"
            )
        if final_dir.exists() or final_dir.is_symlink():
            # 校验与 rename 之间目标被外部放置：内容一致 → 视为 rename
            # 已完成（清理已校验一致的冗余 staging 副本后继续 publish）；
            # 不一致 → fail closed。
            self._verify_node_directory(
                final_dir, expected_files, inventory, stage=stage
            )
            shutil.rmtree(staging_dir)
            _fsync_directory(staging_dir.parent)
            _fsync_directory(session_dir)
            return
        final_dir.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.rename(staging_dir, final_dir)
        except OSError as error:
            raise RuntimeError(
                f"{stage}: staging rename 失败: {staging_dir} -> "
                f"{final_dir}: {error}"
            ) from error
        # durability barrier：rename 后沿 threads/YYYY/MM/DD 父目录链逐级
        # fsync 到 session 目录（新建条目的持久性边界 = owner session 目录，
        # 不越过 session_dir 向上 fsync 共享祖先）。
        chain: Path = final_dir.parent
        while True:
            _fsync_directory(chain)
            if chain == session_dir or chain.parent == chain:
                break
            chain = chain.parent
        _fsync_directory(staging_dir.parent)
        _fsync_directory(session_dir)

    async def _publish_and_finalize(
        self,
        record: ThreadCreationRecord,
        *,
        session_id: str,
        session_dir: Path,
        final_dir: Path,
        expected_files: dict[str, bytes],
        inventory: dict[str, str],
    ) -> ThreadCreationResult:
        """（必要时）gate exclusive 内 CAS publish → admission intent → 复验。

        record 已 ``published``（发布后/terminal 响应前崩溃的重入）时**跳过
        publish**——不重复提交、也绝不把已发布线程送入 CAS 失败清理路径；
        仅补写幂等 admission intent 并复验可见性产物。
        """
        published = record
        if record.state == "preparing":
            published = await self._publish_in_gate(
                record,
                session_id=session_id,
                session_dir=session_dir,
                expected_files=expected_files,
                inventory=inventory,
            )
        # 步骤 5：初始 execution admission intent（publish 后幂等写入；
        # 本轮不绑定真实 Job——state 恒 pending，8.5 幂等 worker 消费）。
        intent = self._control_store.create_or_get_initial_execution_intent(
            admission_idempotency_key=(
                published.thread_creation_idempotency_key
            ),
            session_id=session_id,
            thread_id=published.child_thread_id,
            initial_state=published.initial_state,
            creation_idempotency_key=(
                published.thread_creation_idempotency_key
            ),
        )
        # publish 后 staging 残留 = 外部改动（本流不变量：rename 后 staging
        # 必已不在，见 create 主流程）→ fail closed。
        staging_dir = session_dir / published.staging_locator
        if staging_dir.exists() or staging_dir.is_symlink():
            raise RuntimeError(
                "publish 后 staging 仍残留（外部改动，fail closed）: "
                f"key={published.thread_creation_idempotency_key!r}, "
                f"staging={staging_dir}"
            )
        # 复验最终目录与唯一可见性提交点产物（published 幂等返回的统一
        # 收敛出口）。
        self._verify_node_directory(
            final_dir,
            expected_files,
            inventory,
            stage="published 复验",
        )
        self._control_store.mark_thread_creation_published(
            published.thread_creation_idempotency_key
        )
        return ThreadCreationResult(
            child_thread_id=published.child_thread_id,
            final_relative_locator=published.final_relative_locator,
            record_state=published.state,
            admission_intent=json.loads(published.admission_intent),
            admission_state=intent.state,
            execution_binding_id=intent.execution_binding_id,
            frozen_job_id=intent.job_id,
        )

    async def _publish_in_gate(
        self,
        record: ThreadCreationRecord,
        *,
        session_id: str,
        session_dir: Path,
        expected_files: dict[str, bytes],
        inventory: dict[str, str],
    ) -> ThreadCreationRecord:
        """topology shared → Session gate exclusive 内 CAS publish；
        失败定点清理 + abort。

        publish 临界区**先 fresh 复验 workspace catalog owner 节点仍
        active**：R14 删除流的提交点是 catalog 整树 deleting，而 per-session
        fence 在 gate 外 drain 才关闭，存在「catalog 已 deleting、local
        fence 仍 (active, 冻结 generation)」的真实窗口；仅靠 store 内
        CAS 1（单库）无法跨库发现该窗口，必须在 publish 前重新读取节点
        （fail closed：deleting 时新可见性发布必须取消）。
        """
        idempotency_key = record.thread_creation_idempotency_key
        try:
            async with self._gate.shared(), self._session_gate.exclusive(
                session_id
            ):
                # fresh 复验：workspace catalog owner 节点必须仍 active
                # （与步骤 1 同一投影口径；不重基、不降级）。catalog
                # deleting → 统一 session_deletion_pending 合同（2.3-D）。
                node = self._store.get_node(session_id)
                if node.state == "deleting":
                    raise SessionDeletionPendingError(
                        "session_deletion_pending: workspace catalog owner "
                        "节点已进入删除流，新可见性发布取消（fail closed）: "
                        f"session_id={session_id!r}"
                    )
                if node.state != "active":
                    raise RuntimeError(
                        "thread creation publish 前 workspace catalog owner "
                        "节点非 active（创建/删除双顺序红线，fail closed）: "
                        f"session_id={session_id!r}, state={node.state!r}"
                    )
                published = (
                    self._control_store.publish_thread_creation_record(
                        idempotency_key
                    )
                )
        except SessionDeletionPendingError as error:
            # 2.3-D：删除先行的收敛 = 定点清理 + abort（取消原 operation），
            # 错误类型原样传播（调用方据此终止，不换 key 重放）。
            current = self._control_store.get_thread_creation_record(
                idempotency_key
            )
            if current.state == "preparing":
                self._cleanup_after_failed_publish(
                    record,
                    session_dir=session_dir,
                    expected_files=expected_files,
                    inventory=inventory,
                )
                self._control_store.abort_thread_creation_record(
                    idempotency_key, str(error)
                )
            raise
        except RuntimeError as error:
            # TODO(8.5): 本 except 目前把**全部** RuntimeError 视为确定性
            # publish 冲突并走定点清理 + abort；8.5 扩展 publish 错误面
            # （引入可重试错误）时必须收窄捕获范围——可用
            # ``type(error) is RuntimeError`` 精确捕获确定性冲突，让
            # RuntimeError 子类（可重试类）向调用方传播而不误 abort 换 key。
            # 防御：publish 失败但 record 已 published（gate 期间被同 key
            # 并发/人工推进）→ 绝不清理已发布线程，交由幂等收敛路径处理。
            current = self._control_store.get_thread_creation_record(
                idempotency_key
            )
            if current.state != "preparing":
                raise RuntimeError(
                    "thread creation publish 失败且 record 已非 preparing"
                    f"（state={current.state!r}），不执行定点清理: "
                    f"key={idempotency_key!r}: {error}"
                ) from error
            # CAS 失败：不发布、不重基——按 record 定点清理 staging/final
            # 目录（清理失败保持 record preparing 并直接抛错，人工核账），
            # 再 abort record（8.5-A：调用方以新 operation 重试）。
            self._cleanup_after_failed_publish(
                record,
                session_dir=session_dir,
                expected_files=expected_files,
                inventory=inventory,
            )
            self._control_store.abort_thread_creation_record(
                idempotency_key, str(error)
            )
            raise RuntimeError(
                "thread creation publish CAS 失败，已定点清理 record 列出的"
                "目录并 abort record，调用方须换新 idempotency_key 重试: "
                f"key={idempotency_key!r}: {error}"
            ) from error
        return published

    def _cleanup_after_failed_publish(
        self,
        record: ThreadCreationRecord,
        *,
        session_dir: Path,
        expected_files: dict[str, bytes],
        inventory: dict[str, str],
    ) -> None:
        """CAS 失败后的定点清理：只触碰 record 冻结的 staging/final 目录。

        清理前先校验目录内容确属本 operation（与预期清单一致）——外来
        内容 fail closed（不吸收、不误删），record 保持 preparing。
        """
        stage = "CAS 失败定点清理"
        targets = (
            (session_dir / record.final_relative_locator, "final"),
            (session_dir / record.staging_locator, "staging"),
        )
        for directory, label in targets:
            if not (directory.exists() or directory.is_symlink()):
                continue
            self._verify_node_directory(
                directory,
                expected_files,
                inventory,
                stage=f"{stage}({label})",
            )
            shutil.rmtree(directory)
            _fsync_directory(directory.parent)
