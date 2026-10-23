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
- staging 准备中断时，只接受冻结清单内的文件/目录子集与原子写临时文件；
  持原 holder 定点清理后按同 record 重准备，额外项或内容冲突 fail closed；
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
- **唯一目录权威**：本流只依赖 workspace catalog store（R10）解析 owner
  session 目录与校验 active，只消费 ``get_node`` 的稳定投影。

错误分类约定（沿用 ``session_catalog_store.py``）：``TypeError`` 类型错、
``ValueError`` 形态非法、``KeyError`` 目标不存在、``RuntimeError`` 语义
冲突/外部改动 fail closed。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import shutil
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from app.core.atomic_fs import (
    atomic_write_bytes as _atomic_write_bytes,
)
from app.core.atomic_fs import (
    fsync_directory as _fsync_directory,
)
from app.core.identifier import uuid7_datetime_from_hex
from app.core.key_lock_pool import KeyLockPool
from app.core.phase_observer import observe_phase
from app.core.session_catalog_store import (
    SessionCatalogNode,
    SessionCatalogStore,
    validate_path_budget,
)
from app.core.session_control_primitives import (
    CONTROL_DATABASE_NAME,
    validate_thread_creation_key,
)
from app.core.session_control_store import (
    _INITIAL_STATE_VALUES,
    CollaborationMemberInput,
    SessionControlStore,
    ThreadCreationRecord,
    validate_thread_id,
)
from app.core.session_lifecycle_gate import (
    NavigationTopologyGate,
    OperationHolderGate,
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

# child node 结构文件（确定性内容；清单只覆盖文件，目录集单独断言）。
_THREAD_MANIFEST_NAME = "thread.json"
_ARTIFACT_MANIFEST_NAME = "artifact-manifest.json"
_ARTIFACTS_DIR_NAME = "artifacts"

# child node 必备目录集（runs/ 为初始 execution runs 占位空目录）。
_EXPECTED_NODE_DIRS = ("artifacts",)

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
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
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
            f"graph_binding['graph_revision'] 必须 >= 1: 实际 {graph_revision}"
        )
    for key in sorted(_GRAPH_BINDING_KEYS - {"graph_revision"}):
        value = graph_binding[key]
        if not isinstance(value, str) or not value:
            raise ValueError(f"graph_binding[{key!r}] 必须是非空字符串: {value!r}")
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
        raise ValueError(f"task_reference 必须是 dict 或 None: {task_reference!r}")
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

    路径之间不得语义冲突：同一相对路径既是文件又是另一路径的祖先目录
    （如同时给出 ``x`` 与 ``x/y``），落盘时必然抛裸 ``FileExistsError``
    并留下半成品 staging、重入永久 fail closed，故在准入前明确拒绝。
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
            raise TypeError(f"artifact 内容必须是 str（UTF-8 文本）: {rel_path!r}")
        # 本路径的每个祖先目录都被本文件占用；祖先目录若同时作为另一
        # artifact 的文件路径出现，即 file/dir 语义冲突。逐路径检查即可
        # 覆盖两种给出顺序（后出现者必然看到先前注册的祖先）。
        for depth in range(1, len(segments)):
            ancestor = "/".join(segments[:depth])
            if ancestor in artifact_manifest:
                raise ValueError(
                    "artifact 路径与另一 artifact 互为文件/目录（语义冲突）: "
                    f"{rel_path!r} 的祖先目录 {ancestor!r} 同时是文件"
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
    purpose: str = "task",
    request_intent: dict[str, object] | None = None,
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
        "purpose": purpose,
        "request_intent": request_intent,
    }
    return hashlib.sha256(canonical_json_text(payload).encode("utf-8")).hexdigest()


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
    files: dict[str, bytes] = {
        _THREAD_MANIFEST_NAME: canonical_json_bytes(thread_manifest),
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
    return hashlib.sha256(canonical_json_text(inventory).encode("utf-8")).hexdigest()


def expected_node_directory_names(
    files: dict[str, str],
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


logger = logging.getLogger(__name__)


@contextmanager
def _thread_access(
    session_id: str, thread_id: str | None, stage: str, *, runtime_error: bool = False,
) -> Iterator[None]:
    """物理创建操作只公开逻辑身份与系统码，原错留给管理诊断。"""
    try:
        yield
    except OSError as error:
        detail = (f"Thread 创建存储失败: session_id={session_id}, thread_id={thread_id}, "
                  f"stage={stage}, error={type(error).__name__}, errno={error.errno}")
        if (winerror := getattr(error, "winerror", None)) is not None:
            detail += f", winerror={winerror}"
        logger.error("%s；完整诊断见异常链", detail, exc_info=error)
        if runtime_error:
            raise RuntimeError(detail) from error
        failure = type(error)(error.errno, detail)
        if (winerror := getattr(error, "winerror", None)) is not None:
            failure.winerror = winerror
        raise failure from error


def _thread_conflict(
    session_id: str, thread_id: str, stage: str, reason: str, **diagnostics: object,
) -> RuntimeError:
    """未知目录/正文只进原始日志，校验失败继续保留现场。"""
    detail = f"{stage}: {reason}: session_id={session_id}, thread_id={thread_id}"
    logger.error("%s；原始诊断=%r", detail, diagnostics)
    return RuntimeError(detail)


def verify_node_directory(
    directory: Path,
    inventory: dict[str, str],
    *,
    session_id: str,
    thread_id: str,
    stage: str,
    published: bool = False,
    partial: bool = False,
    materialized_rollout: bool = False,
) -> bool:
    """按冻结清单校验目录、正文及符号链接；冲突保留现场，不吸收未知项。"""
    def reject_walk_error(error: OSError) -> None:
        # os.walk 默认吞掉 scandir 错误；创建核验必须暴露原生失败。
        raise error

    with _thread_access(session_id, thread_id, stage):
        if not directory.is_dir() or directory.is_symlink():
            raise _thread_conflict(session_id, thread_id, stage, "目录缺失或不是目录（外部改动，fail closed）", path=directory)
        expected_file_set = set(inventory) | {_ARTIFACT_MANIFEST_NAME}
        expected_dir_set = expected_node_directory_names(inventory)
        runtime_paths = {"rollout", "debug/node"} if published or materialized_rollout else set()
        if published:
            runtime_paths.add("runs")
        atomic_temporary_present = False
        actual_files: dict[str, bytes] = {}
        actual_dirs: set[str] = set()
        for root, dir_names, file_names in os.walk(directory, followlinks=False, onerror=reject_walk_error):
            root_path = Path(root)
            # 只跳过对应 owner 已发布的精确 runtime 子树；debug 未知兄弟仍校验。
            for dir_name in tuple(dir_names):
                entry = root_path / dir_name
                if entry.is_symlink():
                    raise _thread_conflict(session_id, thread_id, stage, "发现 symlink 目录（外部改动，fail closed）", path=entry)
                relative = entry.relative_to(directory).as_posix()
                if relative in runtime_paths:
                    dir_names.remove(dir_name)
                    if relative == "debug/node":
                        expected_dir_set.add("debug")
                    continue
                actual_dirs.add(relative)
            for file_name in file_names:
                entry = root_path / file_name
                if entry.is_symlink():
                    raise _thread_conflict(session_id, thread_id, stage, "发现 symlink 文件（外部改动，fail closed）", path=entry)
                relative = entry.relative_to(directory).as_posix()
                if (published or materialized_rollout) and relative == ".rollout.write.lock":
                    if not entry.is_file():
                        raise _thread_conflict(session_id, thread_id, stage, "rollout 写锁必须是普通文件", path=entry)
                    continue
                if partial:
                    # 只识别原子写在 replace 前遗留的冻结目标临时项。
                    parent, _, leaf = relative.rpartition("/")
                    if any(
                        expected.rpartition("/")[0] == parent
                        and re.fullmatch(re.escape("." + expected.rpartition("/")[2] + ".") + r"[a-z0-9_]{8}", leaf)
                        for expected in expected_file_set
                    ):
                        if not entry.is_file():
                            raise _thread_conflict(session_id, thread_id, stage, "原子写临时项不是普通文件", path=entry)
                        atomic_temporary_present = True
                        continue
                actual_files[relative] = entry.read_bytes()
        if not (actual_dirs <= expected_dir_set if partial else actual_dirs == expected_dir_set):
            raise _thread_conflict(session_id, thread_id, stage, "目录集与预期不一致（外部改动或上次准备中断，fail closed）",
                                   path=directory, expected=sorted(expected_dir_set), actual=sorted(actual_dirs))
        if not (set(actual_files) <= expected_file_set if partial else set(actual_files) == expected_file_set):
            raise _thread_conflict(session_id, thread_id, stage, "文件集与预期不一致（外部改动或上次准备中断，fail closed）",
                                   path=directory, expected=sorted(expected_file_set), actual=sorted(actual_files))
        for rel_path in sorted(inventory):
            if partial and rel_path not in actual_files:
                continue
            if hashlib.sha256(actual_files[rel_path]).hexdigest() != inventory[rel_path]:
                raise _thread_conflict(session_id, thread_id, stage, "文件 sha256 与预期不一致（外部改动，fail closed）", path=directory / rel_path)
        if partial and _ARTIFACT_MANIFEST_NAME not in actual_files:
            return False
        if actual_files[_ARTIFACT_MANIFEST_NAME] != serialize_artifact_manifest_bytes(inventory):
            raise _thread_conflict(session_id, thread_id, stage, "artifact-manifest.json 字节与预期不一致（外部改动，fail closed）",
                                   path=directory / _ARTIFACT_MANIFEST_NAME)
        return actual_dirs == expected_dir_set and set(actual_files) == expected_file_set and not atomic_temporary_present


async def settle_thread_creations_for_deletion(
    *,
    control: SessionControlStore,
    session_id: str,
    session_dir: Path,
    sessions_root: Path,
    session_gate: SessionLifecycleGate,
    fork_abort: Callable[[str, ThreadCreationRecord], Awaitable[bool]] | None = None,
) -> None:
    """在删除 gate 外等待原 holder，再按同记录取消无执行者的创建。

    catalog 已整体 deleting，禁止新准入；旧执行者仍可短时取得原 gate
    完成 publish 拒绝与定点取消。进程退出释放 holder 后，本入口按冻结
    locator/清单接管取消，不扫盘、不重建或更换创建 identity。
    """
    holders = OperationHolderGate(sessions_root, namespace="thread_creation")
    for pending in control.list_pending_thread_creations():
        key = pending.thread_creation_idempotency_key
        async with holders.lock_for(f"{session_id}/{key}"):
            async with session_gate.exclusive(session_id):
                record = control.get_thread_creation_record(key)
                if record.state != "preparing":
                    continue
                inventory = json.loads(record.artifact_manifest) if record.artifact_manifest is not None else None
                if inventory is not None and compute_artifact_manifest_hash(inventory) != record.artifact_manifest_hash:
                    raise RuntimeError(f"创建取消的冻结清单 hash 冲突: key={key!r}")

            async def abort_fork(current: ThreadCreationRecord) -> bool:
                if fork_abort is None:
                    raise RuntimeError("删除 fork child 缺少原资源 owner 清理回调")
                return await fork_abort(session_id, current)

            await ThreadCreationService._cleanup_after_failed_publish(
                record, session_id=session_id, session_dir=session_dir, inventory=inventory,
                before_abort=abort_fork if record.purpose == "fork" else None,
            )
            async with session_gate.exclusive(session_id):
                if control.get_thread_creation_record(key) != record:
                    raise RuntimeError("删除取消的创建 record 在 holder 内变化")
                control.abort_thread_creation_record(key, "session_deletion_pending")
            await observe_phase(key, "thread.creation.cancelled")


@dataclass(frozen=True, slots=True)
class ThreadCreationResult:
    """一次 child thread 创建流的最终结果（record 已 published 的投影）。"""

    child_thread_id: str
    final_relative_locator: str
    record_state: str
    admission_intent: dict[str, object] | None
    admission_state: str | None
    execution_binding_id: str | None
    frozen_job_id: str | None


class ThreadCreationAbortedError(RuntimeError):
    """原创建 owner 已完成中止；恢复调用方可核验其持久终态。"""

    def __init__(self, record: ThreadCreationRecord) -> None:
        self.record = record
        super().__init__(
            "thread creation record 已中止，调用方须换新 "
            f"idempotency_key 重试: key={record.thread_creation_idempotency_key!r}, "
            f"reason={record.abort_reason!r}"
        )


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
        self._key_locks = KeyLockPool()
        self._holders = OperationHolderGate(self._sessions_root, namespace="thread_creation")

    @property
    def control_store(self) -> SessionControlStore:
        """Session execution worker 的唯一 control owner 端口。"""
        return self._control_store

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
        purpose: str = "task",
        request_intent: dict[str, object] | None = None,
        fork_delivery_request_id: str | None = None,
        before_publish: Callable[[ThreadCreationRecord], Awaitable[None]] | None = None,
        before_abort: Callable[[ThreadCreationRecord], Awaitable[bool]] | None = None,
        on_published: Callable[[ThreadCreationRecord], None] | None = None,
    ) -> ThreadCreationResult:
        """执行（或幂等恢复）一次 child thread 创建（协议见类 docstring）。

        ``thread_id=None`` 时 child ID 由 store 自然分配（``thr_``）；提供
        时其 UUIDv7 时间戳作为固定 ID 的创建时刻。``delegation_id`` 提供即
        为 delegated child（纳入 preimage 与部分唯一约束）。
        """
        if purpose not in {"task", "fork"}:
            raise ValueError(f"thread creation purpose 非法: {purpose}")
        if purpose == "fork" and (request_intent is None or before_publish is None or before_abort is None or initial_state != "idle"
                                  or delegation_id is not None or collaboration_member is not None):
            raise ValueError("fork child 必须冻结请求、先完成物化、保持 idle 且没有 delegation/member")
        if purpose == "task" and any(value is not None for value in (request_intent, fork_delivery_request_id, before_publish, before_abort, on_published)):
            raise ValueError("task child 不接受 fork 物化合同")
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
            purpose=purpose,
            request_intent=request_intent,
        )
        # 进程内同 key 串行：并发同 key create 收敛到同一结果。
        await observe_phase(idempotency_key, "thread.creation.before_admission", details={
            "session_id": session_id, "thread_id": thread_id,
        })
        async with self._key_lock(idempotency_key), self._holders.lock_for(f"{session_id}/{idempotency_key}"):
            # 步骤 1：准入 = topology shared → Session gate exclusive（2.3-A
            # 固定锁序），fresh 验证后冻结 record（等价 operation lease）。
            async with self._gate.shared(), self._session_gate.exclusive(session_id):
                node = self._store.get_node(session_id)
                self._validate_owner_node(node)
                session_dir = self._session_dir_for(node)
                self._validate_control_store_binding(session_dir)
                self._control_store.verify_matches_catalog_main_thread(
                    str(node.main_thread_id)
                )
                # 落盘前先校验路径预算与 artifact 语义：超限/冲突的输入若
                # 拖到 staging 准备阶段才失败，会留下裸 OSError 与半成品
                # staging，且 record 已按同 preimage 冻结、重入永久 fail
                # closed。故在冻结 record 前（任何状态变更之前）明确拒绝。
                self._validate_filesystem_budget(
                    session_dir=session_dir, session_id=session_id,
                    idempotency_key=idempotency_key,
                    artifact_manifest=artifact_manifest,
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
                    created_at=(
                        uuid7_datetime_from_hex(thread_id[4:])
                        if thread_id is not None
                        else None
                    ),
                    thread_id=thread_id,
                    delegation_id=delegation_id,
                    owner_session_id=session_id,
                    collaboration_member=(CollaborationMemberInput(
                        coordinator_session_id=session_id,
                        coordinator_thread_id=str(session_metadata["task_reference"]["parent_thread_id"]),
                        role=str(collaboration_member["role"]),
                        subagent_type=str(collaboration_member["subagent_type"]),
                        title=str(collaboration_member["title"]),
                    ) if collaboration_member is not None else None),
                    purpose=purpose,
                    request_intent_json=canonical_json_text(request_intent) if request_intent is not None else None,
                    fork_delivery_request_id=fork_delivery_request_id,
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
            await observe_phase(idempotency_key, "thread.creation.prepared")
            if record.state == "aborted":
                raise ThreadCreationAbortedError(record)
            # 步骤 2/3：staging 准备 + 原子 rename（gate 外）。
            staging_dir = session_dir / record.staging_locator
            final_dir = session_dir / record.final_relative_locator
            expected_files = build_thread_node_files(
                record=record,
                session_id=session_id,
                artifact_manifest=artifact_manifest,
            )
            inventory = build_thread_node_inventory(expected_files)
            with _thread_access(session_id, record.child_thread_id, "locate"):
                final_exists = final_dir.exists() or final_dir.is_symlink()
            if record.purpose == "fork" and record.state == "preparing":
                capture = self._store.get_fork_source_capture(creation_key=idempotency_key)
                if capture is not None:
                    request = json.loads(capture.request_json)
                    if (capture.copy_operation_id, capture.creation_key, capture.target_session_id,
                        capture.target_thread_id, request["creation_kind"], request["creation_preimage_hash"]) != (
                        idempotency_key, idempotency_key, session_id, record.child_thread_id, "new_child_thread", record.preimage_hash,
                    ):
                        raise RuntimeError("fork child capture 与原 creation owner 冲突，拒绝恢复目录")
                    if capture.state == "aborted":
                        # 资源先中止、目录后删除、creation 最后终结；崩溃后只接续原清理。
                        await self._cleanup_after_failed_publish(
                            record, session_id=session_id, session_dir=session_dir, inventory=inventory,
                            before_abort=before_abort,
                        )
                        aborted = self._control_store.abort_thread_creation_record(
                            idempotency_key, capture.abort_reason or "fork capture 已中止",
                        )
                        raise ThreadCreationAbortedError(aborted)
                    if not final_exists:
                        # capture 只能在 final rename 后预留，缺目录不是创建前的合法窗口。
                        raise RuntimeError(
                            "fork child 已预留 capture 但最终 locator 目录缺失（外部删除，拒绝重建）: "
                            f"session_id={session_id}, thread_id={record.child_thread_id}"
                        )
            if record.state == "published" and not final_exists:
                # published 的 record 其 final 目录已被外部删除：**不重建**。
                # rename 先于 publish，协议内崩溃点不可能产生该状态；重建
                # 等于静默吸收外部对可见性产物的改动（对齐会话目录「绕过
                # 软件修改必须明确报错」原则）。
                raise RuntimeError(
                    "thread creation record 已 published 但最终 locator 目录"
                    "缺失（外部删除可见性产物，fail closed，拒绝重建）: "
                    f"session_id={session_id}, thread_id={record.child_thread_id}"
                )
            if final_exists:
                # rename 后、publish 前崩溃的恢复窗口：目标内容一致才继续。
                if record.purpose == "fork" and record.state == "preparing":
                    if before_publish is None:
                        raise RuntimeError("fork child 缺少物化 owner")
                    record = await self._materialize_prepared_fork(
                        record, session_id=session_id, session_dir=session_dir, inventory=inventory,
                        before_publish=before_publish, before_abort=before_abort,
                    )
                verify_node_directory(
                    final_dir,
                    inventory,
                    session_id=session_id, thread_id=record.child_thread_id, stage="恢复(最终 locator 已存在)",
                    published=record.state == "published",
                    materialized_rollout=self._has_fork_completion(record, session_id),
                )
                with _thread_access(session_id, record.child_thread_id, "locate"):
                    staging_exists = staging_dir.exists() or staging_dir.is_symlink()
                if staging_exists:
                    raise RuntimeError(
                        "最终 locator 与 staging 同时存在（外部改动，fail "
                        f"closed）: key={idempotency_key!r}, "
                        f"session_id={session_id}, thread_id={record.child_thread_id}"
                    )
                return await self._publish_and_finalize(
                    record,
                    session_id=session_id,
                    session_dir=session_dir,
                    final_dir=final_dir,
                    inventory=inventory,
                    before_abort=before_abort,
                    on_published=on_published,
                )
            self._prepare_staging(staging_dir, session_dir, expected_files, inventory, record, session_id)
            self._rename_staging_to_final(
                staging_dir,
                final_dir,
                session_dir,
                inventory, record, session_id,
            )
            if record.purpose == "fork":
                if before_publish is None:
                    raise RuntimeError("fork child 缺少物化 owner")
                record = await self._materialize_prepared_fork(
                    record, session_id=session_id, session_dir=session_dir, inventory=inventory,
                    before_publish=before_publish, before_abort=before_abort,
                )
            # 步骤 4/5：gate exclusive 内 CAS publish + admission intent。
            return await self._publish_and_finalize(
                record,
                session_id=session_id,
                session_dir=session_dir,
                final_dir=final_dir,
                inventory=inventory,
                before_abort=before_abort,
                on_published=on_published,
            )

    async def _materialize_prepared_fork(
        self, record: ThreadCreationRecord, *, session_id: str, session_dir: Path,
        inventory: dict[str, str], before_publish: Callable[[ThreadCreationRecord], Awaitable[None]],
        before_abort: Callable[[ThreadCreationRecord], Awaitable[bool]] | None,
    ) -> ThreadCreationRecord:
        key = record.thread_creation_idempotency_key
        try:
            await before_publish(record)
        except Exception as error:
            current = self._control_store.get_thread_creation_record(key)
            if current.state == "preparing":
                await self._cleanup_after_failed_publish(
                    current, session_id=session_id, session_dir=session_dir, inventory=inventory,
                    before_abort=before_abort,
                )
                self._control_store.abort_thread_creation_record(key, str(error))
            raise
        return self._control_store.get_thread_creation_record(key)

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
        # idempotency_key 是 session 内 .staging/ 目录名：与 store 侧共用
        # 同一口径（session_control_primitives 单点定义），服务层先行
        # fail fast。
        validate_thread_creation_key(idempotency_key)
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
            compute_capability_profile_hash=(self._compute_capability_profile_hash),
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
        if (delegation_id is None) != (collaboration_member is None):
            raise ValueError("delegated creation 必须同时提供 delegation_id 与 collaboration_member")
        if collaboration_member is None:
            return
        if delegation_id is None:
            raise ValueError(
                "collaboration member 登记必须携带 delegation_id（manual "
                f"creation 不登记）: key={idempotency_key!r}"
            )
        if not isinstance(collaboration_member, dict) or set(collaboration_member) != {
            "role",
            "subagent_type",
            "title",
        }:
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

    def _validate_filesystem_budget(
        self,
        *,
        session_dir: Path,
        session_id: str,
        idempotency_key: str,
        artifact_manifest: dict[str, object],
    ) -> None:
        """在冻结 record 前校验落盘路径的组件/总长预算。

        staging 目录名（幂等键）与每个 artifact 相对路径都必须在落盘前
        落在文件系统预算内（组件 ≤255 bytes、总长 ≤4096 bytes）；否则
        ``_prepare_staging`` 会抛裸 ``OSError`` 并留下半成品 staging，而
        record 已按输入 preimage 冻结、重入永久 fail closed（拒绝服务）。
        预算口径复用 ``session_catalog_store.validate_path_budget``，不复制
        第二份常量。

        artifact 的最终落位是
        ``<session_dir>/threads/YYYY/MM/DD/thr_<32hex>/artifacts/<rel>``；
        ``thread_id``（恒定 36 字符）与日期桶（恒定 10 字符）长度固定，用
        等长占位符即可精确复现总长预算。
        """
        with _thread_access(session_id, None, "path_budget"):
            try:
                validate_path_budget(session_dir, f"{_STAGING_DIR_NAME}/{idempotency_key}")
                node_locator = "threads/0000/00/00/" + "thr_" + "0" * 32
                for rel_path in artifact_manifest:
                    validate_path_budget(
                        session_dir,
                        f"{node_locator}/{_ARTIFACTS_DIR_NAME}/{rel_path}",
                    )
            except (ValueError, RuntimeError) as error:
                detail = (f"Thread 创建路径预算校验失败: session_id={session_id}, "
                          f"stage=path_budget, error={type(error).__name__}")
                logger.error("%s；完整预算与位置见异常链", detail, exc_info=error)
                raise type(error)(detail) from error

    def _key_lock(self, idempotency_key: str) -> asyncio.Lock:
        """按 key 取进程内串行锁（固定分片，同 key 恒命中同一把）。"""
        return self._key_locks.lock_for(idempotency_key)

    # ------------------------------------------------------------------
    # owner session 定位与绑定校验
    # ------------------------------------------------------------------

    def _validate_owner_node(self, node: SessionCatalogNode) -> None:
        """owner session 节点校验：session 类型、active、同 workspace。"""
        if node.kind != "session":
            raise ValueError(
                f"owner 节点不是 session: node_id={node.node_id!r}, kind={node.kind!r}"
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
        return self._sessions_root / locator[len("sessions/") :]

    def _validate_control_store_binding(self, session_dir: Path) -> None:
        """control store 必须绑定 owner session 目录（误绑 fail fast）。"""
        expected_path = (session_dir / CONTROL_DATABASE_NAME).resolve()
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
        record: ThreadCreationRecord,
        session_id: str,
    ) -> None:
        """staging 已存在 → 逐文件校验复用；不存在 → 完整准备 + barrier。"""
        with _thread_access(session_id, record.child_thread_id, "staging_prepare"):
            staging_exists = staging_dir.exists() or staging_dir.is_symlink()
        if staging_exists:
            if verify_node_directory(
                staging_dir, inventory, session_id=session_id, thread_id=record.child_thread_id,
                stage="staging 复验", partial=True,
            ):
                return
            # 原 holder 已取得；合法准备中断只按本 record 的 staging 重建。
            with _thread_access(session_id, record.child_thread_id, "staging_rebuild"):
                shutil.rmtree(staging_dir)
                _fsync_directory(staging_dir.parent)
        with _thread_access(session_id, record.child_thread_id, "staging_prepare"):
            staging_dir.mkdir(parents=True, exist_ok=False)
            for dir_name in _EXPECTED_NODE_DIRS:
                (staging_dir / dir_name).mkdir(exist_ok=False)
            for rel_path, payload in expected_files.items():
                _atomic_write_bytes(staging_dir / rel_path, payload)
            _atomic_write_bytes(
                staging_dir / _ARTIFACT_MANIFEST_NAME,
                serialize_artifact_manifest_bytes(inventory),
            )
            # 文件原子写已 fsync；补目录与父挂点 durability barrier。
            for dir_name in _EXPECTED_NODE_DIRS:
                _fsync_directory(staging_dir / dir_name)
            _fsync_directory(staging_dir)
            _fsync_directory(staging_dir.parent)
            _fsync_directory(session_dir)
        verify_node_directory(
            staging_dir, inventory, session_id=session_id, thread_id=record.child_thread_id,
            stage="staging 落盘对账",
        )

    # ------------------------------------------------------------------
    # rename 与 publish
    # ------------------------------------------------------------------

    def _rename_staging_to_final(
        self,
        staging_dir: Path,
        final_dir: Path,
        session_dir: Path,
        inventory: dict[str, str],
        record: ThreadCreationRecord,
        session_id: str,
    ) -> None:
        """staging 原子 rename 到冻结最终 locator（父目录链 mkdir + fsync）。"""
        stage = "rename 到最终 locator"
        with _thread_access(session_id, record.child_thread_id, "rename_prepare"):
            if not staging_dir.is_dir() or staging_dir.is_symlink():
                raise _thread_conflict(session_id, record.child_thread_id, stage, "staging 缺失或不是目录", path=staging_dir)
            final_exists = final_dir.exists() or final_dir.is_symlink()
        if final_exists:
            # 外部放置的同内容 final 可复用；先核实，再清理冗余 staging。
            verify_node_directory(final_dir, inventory, session_id=session_id, thread_id=record.child_thread_id, stage=stage)
            with _thread_access(session_id, record.child_thread_id, "rename_redundant_staging"):
                shutil.rmtree(staging_dir)
                _fsync_directory(staging_dir.parent)
                _fsync_directory(session_dir)
            return
        with _thread_access(session_id, record.child_thread_id, "rename_prepare"):
            final_dir.parent.mkdir(parents=True, exist_ok=True)
        with _thread_access(session_id, record.child_thread_id, "rename", runtime_error=True):
            os.rename(staging_dir, final_dir)
        # rename 后沿新父目录链 fsync 到 owner Session，不越过共享祖先。
        with _thread_access(session_id, record.child_thread_id, "rename_durability"):
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
        inventory: dict[str, str],
        before_abort: Callable[[ThreadCreationRecord], Awaitable[bool]] | None = None,
        on_published: Callable[[ThreadCreationRecord], None] | None = None,
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
                inventory=inventory,
                before_abort=before_abort,
                on_published=on_published,
            )
        elif on_published is not None:
            async with self._gate.shared(), self._session_gate.exclusive(session_id):
                on_published(published)
        # 步骤 5：初始 execution admission intent（publish 后幂等写入；
        # 本轮不绑定真实 Job——state 恒 pending，8.5 幂等 worker 消费）。
        intent = None
        if published.purpose == "task":
            intent = self._control_store.get_initial_execution_intent(
                published.thread_creation_idempotency_key
            )
            if intent.session_id != session_id or intent.thread_id != published.child_thread_id:
                raise RuntimeError("publication 的初始 execution intent owner 不一致")
        # publish 后 staging 残留 = 外部改动（本流不变量：rename 后 staging
        # 必已不在，见 create 主流程）→ fail closed。
        staging_dir = session_dir / published.staging_locator
        with _thread_access(session_id, published.child_thread_id, "published_staging"):
            if staging_dir.exists() or staging_dir.is_symlink():
                raise RuntimeError(
                    "publish 后 staging 仍残留（外部改动，fail closed）: "
                    f"key={published.thread_creation_idempotency_key!r}, "
                    f"session_id={session_id}, thread_id={published.child_thread_id}"
                )
        # 复验最终目录与唯一可见性提交点产物（published 幂等返回的统一
        # 收敛出口）。
        verify_node_directory(
            final_dir,
            inventory,
            session_id=session_id, thread_id=published.child_thread_id, stage="published 复验",
            published=True,
        )
        self._control_store.mark_thread_creation_published(
            published.thread_creation_idempotency_key
        )
        return ThreadCreationResult(
            child_thread_id=published.child_thread_id,
            final_relative_locator=published.final_relative_locator,
            record_state=published.state,
            admission_intent=json.loads(published.admission_intent) if published.admission_intent is not None else None,
            admission_state=intent.state if intent is not None else None,
            execution_binding_id=intent.execution_binding_id if intent is not None else None,
            frozen_job_id=intent.job_id if intent is not None else None,
        )

    async def _publish_in_gate(
        self,
        record: ThreadCreationRecord,
        *,
        session_id: str,
        session_dir: Path,
        inventory: dict[str, str],
        before_abort: Callable[[ThreadCreationRecord], Awaitable[bool]] | None = None,
        on_published: Callable[[ThreadCreationRecord], None] | None = None,
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
            async with self._gate.shared(), self._session_gate.exclusive(session_id):
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
                published = self._control_store.publish_thread_creation_record(
                    idempotency_key, session_id=session_id
                )
                if on_published is not None:
                    on_published(published)
                await observe_phase(idempotency_key, "thread.creation.published")
        except SessionDeletionPendingError as error:
            # 2.3-D：删除先行的收敛 = 定点清理 + abort（取消原 operation），
            # 错误类型原样传播（调用方据此终止，不换 key 重放）。
            current = self._control_store.get_thread_creation_record(idempotency_key)
            if current.state == "preparing":
                await self._cleanup_after_failed_publish(
                    current,
                    session_id=session_id,
                    session_dir=session_dir,
                    inventory=inventory,
                    before_abort=before_abort,
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
            current = self._control_store.get_thread_creation_record(idempotency_key)
            if current.state == "published":
                # 跨执行根（多进程 GUI/worker/CLI）重复投递同一
                # idempotency_key：进程内 asyncio.Lock 只保护本进程，另一
                # 实例可能已完成唯一可见性提交点。按 create-or-get 语义
                # 幂等恢复同一结果；但仅当身份逐字段一致才算命中——key
                # 相同内容不同（preimage/child 漂移）必须 fail closed，绝不
                # 静默复用他人线程。绝不走定点清理/abort（那是给真失败准备
                # 的路，会误删/误 abort 已发布线程）。
                if (
                    current.preimage_hash != record.preimage_hash
                    or current.child_thread_id != record.child_thread_id
                ):
                    raise RuntimeError(
                        "thread creation publish 失败：record 已 published "
                        "但身份与本次请求不一致（同 key 不同内容，fail "
                        "closed，拒绝复用）: "
                        f"key={idempotency_key!r}, "
                        f"existing_preimage={current.preimage_hash!r}, "
                        f"requested_preimage={record.preimage_hash!r}, "
                        f"existing_child={current.child_thread_id!r}, "
                        f"requested_child={record.child_thread_id!r}"
                    ) from error
                return current
            if current.state != "preparing":
                # aborted（或未知非 preparing 态）：不清理、原样失败。
                raise RuntimeError(
                    "thread creation publish 失败且 record 已非 preparing"
                    f"（state={current.state!r}），不执行定点清理: "
                    f"key={idempotency_key!r}: {error}"
                ) from error
            # CAS 失败：不发布、不重基——按 record 定点清理 staging/final
            # 目录（清理失败保持 record preparing 并直接抛错，人工核账），
            # 再 abort record（8.5-A：调用方以新 operation 重试）。
            await self._cleanup_after_failed_publish(
                current,
                session_id=session_id,
                session_dir=session_dir,
                inventory=inventory,
                before_abort=before_abort,
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

    @staticmethod
    def _has_fork_completion(record: ThreadCreationRecord, session_id: str) -> bool:
        if record.purpose != "fork" or record.fork_completion_json is None:
            return False
        proof = json.loads(record.fork_completion_json)
        if (
            set(proof) != {"fork_id", "materialization_id", "source_snapshot_sha256", "target_session_id", "target_thread_id"}
            or proof["target_session_id"] != session_id
            or proof["target_thread_id"] != record.child_thread_id
            or not all(isinstance(value, str) and value for value in proof.values())
            or re.fullmatch(r"[0-9a-f]{64}", proof["source_snapshot_sha256"]) is None
        ):
            raise RuntimeError("fork child completion 与本 operation 的 owner/快照不一致")
        return True

    @staticmethod
    async def _cleanup_after_failed_publish(
        record: ThreadCreationRecord,
        *,
        session_id: str,
        session_dir: Path,
        inventory: dict[str, str] | None,
        before_abort: Callable[[ThreadCreationRecord], Awaitable[bool]] | None = None,
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
        materialized_rollout = False
        if record.purpose == "fork":
            if before_abort is None:
                raise RuntimeError("fork child 清理缺少资源 owner 回调")
            materialized_rollout = await before_abort(record)
        for directory, label in targets:
            with _thread_access(session_id, record.child_thread_id, "cleanup_verify"):
                exists = directory.exists() or directory.is_symlink()
            if not exists:
                continue
            if inventory is None:
                raise _thread_conflict(session_id, record.child_thread_id, stage, "未冻结清单的创建已有目录", path=directory)
            verify_node_directory(
                directory, inventory, session_id=session_id, thread_id=record.child_thread_id,
                stage=f"{stage}({label})", partial=label == "staging",
                materialized_rollout=label == "final" and materialized_rollout,
            )
        with _thread_access(session_id, record.child_thread_id, "cleanup"):
            for directory, _label in targets:
                if not (directory.exists() or directory.is_symlink()):
                    continue
                shutil.rmtree(directory)
                _fsync_directory(directory.parent)
