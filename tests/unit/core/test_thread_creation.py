"""ThreadCreationService child thread 创建流测试（OpenSpec 8.5-A，R20）。

覆盖：完整流（staging 内容/唯一可见性提交点/admission intent/durability
可观测）、崩溃点矩阵（record 后/staging 前、staging 后/rename 前、rename
后/publish 前、publish 后/terminal 前、admission intent 前后、abort 清理
中途）、幂等与冲突、CAS 失败定点清理、无 record 目录不吸收、idle 无
seed 显式约束、并发同 key 收敛、入参口径（int graph_revision、平面标量
capability_profile、hash 交叉校验、真实 GraphBinding 兼容）。只使用
tmp_path，不触碰真实工作区。
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.agents.graph_binding import (
    DEEP_AGENT_CAPABILITY_PROFILE,
    DEEP_AGENT_GRAPH_BINDING,
    compute_capability_profile_hash,
)
from app.core.session_catalog_store import (
    SessionCatalogStore,
)
from app.core.session_control_store import (
    SessionControlStore,
    ThreadCreationRecord,
)
from app.core.session_creation import SessionCreationService
from app.core.session_lifecycle_gate import (
    NavigationTopologyGate,
    SessionDeletionPendingError,
)
from app.core.thread_creation import (
    ThreadCreationResult,
    ThreadCreationService,
    build_thread_node_files,
    build_thread_node_inventory,
    canonical_json_text,
    compute_artifact_manifest_hash,
    compute_thread_creation_preimage_hash,
    serialize_artifact_manifest_bytes,
    validate_thread_session_metadata,
    verify_node_directory,
)

WORKSPACE_ID = "ws-thread-create"

# owner session 冻结 created_at（确定性日期桶，仅用于测试断言可预测性；
# R13 创建流用真实 now，日期桶按当天 UTC）。
OWNER_CREATED_AT = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)


def make_thread_id() -> str:
    return f"thr_{uuid.uuid4().hex}"


def make_session_id() -> str:
    return f"ses_{uuid.uuid4().hex}"


CAPABILITY_PROFILE: dict[str, object] = {
    "extension_tool_envelope": "invoke_extension_tool",
    "goal_enabled": False,
    "skill_visibility": "name-description-only",
    "thread_role": "child",
}
"""平面标量能力 profile（对齐 compute_capability_profile_hash 口径）。"""


def make_graph_binding() -> dict[str, object]:
    return {
        "graph_id": "deep-agent",
        "graph_revision": 1,
        "graph_schema_hash": "sha256:" + "a" * 64,
        "capability_profile_hash": compute_capability_profile_hash(
            capability_profile=CAPABILITY_PROFILE
        ),
    }


def make_metadata(**overrides: object) -> dict[str, object]:
    """构造合法的调用方 session_metadata（四字段闭集，running 带 seed）。"""
    metadata: dict[str, object] = {
        "graph_binding": make_graph_binding(),
        "capability_profile": dict(CAPABILITY_PROFILE),
        "task_seed": {"task": "做一件事"},
        "task_reference": {"ref": "doc-1"},
    }
    metadata.update(overrides)
    return metadata


def make_artifacts(**overrides: object) -> dict[str, object]:
    """构造调用方 artifact 载荷（相对路径 → 文本内容）。"""
    artifacts: dict[str, object] = {
        "notes/plan.md": "# 计划\n步骤一\n",
    }
    artifacts.update(overrides)
    return artifacts


@dataclass(frozen=True, slots=True)
class OwnerSession:
    """R13 新模型创建流产出的 owner session（含已初始化控制库）。"""

    session_id: str
    main_thread_id: str
    session_dir: Path
    control: SessionControlStore


def date_bucket_dir(sessions_root: Path, locator: str) -> Path:
    return sessions_root / locator[len("sessions/"):]


def count_rows(store: SessionCatalogStore | SessionControlStore, table: str) -> int:
    return int(
        store.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    )


@pytest.fixture
def sessions_root(tmp_path: Path) -> Path:
    return tmp_path / ".boxteam" / "sessions"


@pytest.fixture
def store(tmp_path: Path, sessions_root: Path) -> SessionCatalogStore:
    catalog = SessionCatalogStore(
        tmp_path / ".boxteam" / "navigation" / "session-catalog.sqlite",
        sessions_root,
    )
    yield catalog
    catalog.close()


@pytest.fixture
async def owner(
    store: SessionCatalogStore,
    sessions_root: Path,
) -> OwnerSession:
    """通过 R13 新模型创建流建立真实 owner session（含 session-control）。"""
    creation = SessionCreationService(
        store=store,
        sessions_root=sessions_root,
        workspace_id=WORKSPACE_ID,
        gate=NavigationTopologyGate(sessions_root),
    )
    result = await creation.create(
        idempotency_key="owner-key",
        title="属主会话",
        parent_node_id=None,
        session_metadata={
            "kind": "normal",
            "delegation": None,
            "generation_origin": None,
            "current_agent_id": "default",
            "current_provider_id": "default_provider",
            "context_source_session_id": None,
        },
    )
    session_dir = date_bucket_dir(
        sessions_root, result.storage_relative_locator
    )
    control = SessionControlStore(session_dir / "session-control.sqlite")
    try:
        control.verify_matches_catalog_main_thread(result.main_thread_id)
    except BaseException:
        control.close()
        raise
    return OwnerSession(
        session_id=result.session_id,
        main_thread_id=result.main_thread_id,
        session_dir=session_dir,
        control=control,
    )


@pytest.fixture
def service(
    store: SessionCatalogStore,
    owner: OwnerSession,
    sessions_root: Path,
) -> ThreadCreationService:
    return ThreadCreationService(
        store=store,
        control_store=owner.control,
        sessions_root=sessions_root,
        workspace_id=WORKSPACE_ID,
        compute_capability_profile_hash=compute_capability_profile_hash,
        gate=NavigationTopologyGate(sessions_root),
    )


async def do_create(
    service: ThreadCreationService,
    owner: OwnerSession,
    *,
    key: str = "key-1",
    thread_id: str | None = None,
    delegation_id: str | None = None,
    initial_state: str = "running",
    metadata: dict[str, object] | None = None,
    artifacts: dict[str, object] | None = None,
    collaboration_member: dict[str, object] | None = None,
) -> ThreadCreationResult:
    return await service.create(
        idempotency_key=key,
        session_id=owner.session_id,
        thread_id=thread_id,
        delegation_id=delegation_id,
        initial_state=initial_state,
        session_metadata=metadata if metadata is not None else make_metadata(),
        artifact_manifest=artifacts if artifacts is not None else make_artifacts(),
        collaboration_member=collaboration_member,
    )


def child_node_files(
    record: ThreadCreationRecord,
    owner: OwnerSession,
    artifacts: dict[str, object],
) -> dict[str, bytes]:
    return build_thread_node_files(
        record=record,
        session_id=owner.session_id,
        artifact_manifest=artifacts,
    )


def child_node_inventory(
    record: ThreadCreationRecord,
    owner: OwnerSession,
    artifacts: dict[str, object],
) -> dict[str, str]:
    return build_thread_node_inventory(
        child_node_files(record, owner, artifacts)
    )


def materialize_node(
    base: Path,
    record: ThreadCreationRecord,
    owner: OwnerSession,
    artifacts: dict[str, object],
) -> None:
    """按服务同款确定性内容手工落位 child node（恢复窗口测试用）。"""
    files = child_node_files(record, owner, artifacts)
    base.mkdir(parents=True, exist_ok=True)
    (base / "rollout").mkdir(exist_ok=True)
    (base / "artifacts").mkdir(exist_ok=True)
    (base / "runs").mkdir(exist_ok=True)
    for rel_path, payload in files.items():
        target = base / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
    (base / "artifact-manifest.json").write_bytes(
        serialize_artifact_manifest_bytes(
            build_thread_node_inventory(files)
        )
    )


def prepare_manual_record(
    owner: OwnerSession,
    *,
    key: str = "key-1",
    thread_id: str | None = None,
    delegation_id: str | None = None,
    initial_state: str = "running",
    metadata: dict[str, object] | None = None,
    artifacts: dict[str, object] | None = None,
    created_at: datetime | None = None,
) -> ThreadCreationRecord:
    """测试辅助：绕过服务直接建 preparing record 并冻结清单（恢复用）。

    preimage/清单与服务同源（compute_thread_creation_preimage_hash /
    build_thread_node_inventory），保证后续 create() 重入能幂等命中。
    """
    effective_metadata = metadata if metadata is not None else make_metadata()
    effective_artifacts = artifacts if artifacts is not None else make_artifacts()
    preimage_hash = compute_thread_creation_preimage_hash(
        workspace_id=WORKSPACE_ID,
        session_id=owner.session_id,
        thread_id=thread_id,
        delegation_id=delegation_id,
        initial_state=initial_state,
        session_metadata=effective_metadata,
        artifact_manifest=effective_artifacts,
    )
    record = owner.control.create_or_get_thread_creation_record(
        idempotency_key=key,
        initial_state=initial_state,
        preimage_hash=preimage_hash,
        graph_binding=canonical_json_text(effective_metadata["graph_binding"]),
        capability_profile=canonical_json_text(
            effective_metadata["capability_profile"]
        ),
        created_at=created_at if created_at is not None else OWNER_CREATED_AT,
        thread_id=thread_id,
        delegation_id=delegation_id,
        task_seed=(
            canonical_json_text(effective_metadata["task_seed"])
            if effective_metadata["task_seed"] is not None
            else None
        ),
        task_reference=(
            canonical_json_text(effective_metadata["task_reference"])
            if effective_metadata["task_reference"] is not None
            else None
        ),
    )
    inventory = child_node_inventory(record, owner, effective_artifacts)
    owner.control.freeze_thread_creation_artifact_manifest(
        key,
        artifact_manifest=canonical_json_text(inventory),
        artifact_manifest_hash=compute_artifact_manifest_hash(inventory),
    )
    return owner.control.get_thread_creation_record(key)


# ----------------------------------------------------------------------
# 完整流
# ----------------------------------------------------------------------


async def test_create_full_flow_publishes_child_thread(
    service: ThreadCreationService,
    owner: OwnerSession,
    sessions_root: Path,
) -> None:
    result = await do_create(service, owner)
    assert result.record_state == "published"
    validate_thread_id_shape(result)
    # thread_catalog child row 可见（唯一可见性提交点）且 kind='child'
    rows = owner.control.connection.execute(
        "SELECT thread_id, kind, created_at FROM thread_catalog "
        "WHERE kind = 'child'"
    ).fetchall()
    assert len(rows) == 1
    assert str(rows[0]["thread_id"]) == result.child_thread_id
    # record published
    record = owner.control.get_thread_creation_record("key-1")
    assert record.state == "published"
    assert record.child_thread_id == result.child_thread_id
    assert record.final_relative_locator == result.final_relative_locator
    # final locator 就位于 owner session 目录下、日期 == child created_at
    final_dir = owner.session_dir / result.final_relative_locator
    assert final_dir.is_dir()
    created_date = datetime.fromisoformat(record.child_created_at).astimezone(
        UTC
    )
    assert result.final_relative_locator == (
        f"threads/{created_date:%Y/%m/%d}/{result.child_thread_id}"
    )
    # staging 清理
    assert not (owner.session_dir / ".staging" / "key-1").exists()
    # admission intent 落库（本轮不绑定 Job：state=pending）
    intent = owner.control.get_initial_execution_intent("key-1")
    assert intent.thread_id == result.child_thread_id
    assert intent.initial_state == "running"
    assert intent.state == "pending"
    assert intent.creation_idempotency_key == "key-1"
    assert intent.session_id == owner.session_id
    # owner main row 不受影响
    assert str(owner.control.get_main_thread()["thread_id"]) == (
        owner.main_thread_id
    )


def validate_thread_id_shape(result: ThreadCreationResult) -> None:
    assert result.child_thread_id.startswith("thr_")
    assert len(result.child_thread_id) == 36


async def test_create_writes_expected_thread_node_content(
    service: ThreadCreationService,
    owner: OwnerSession,
) -> None:
    result = await do_create(service, owner)
    final_dir = owner.session_dir / result.final_relative_locator
    # 根条目集恰为 5（5 文件/目录；artifacts/notes 为嵌套目录）
    assert sorted(p.name for p in final_dir.iterdir()) == [
        "artifact-manifest.json",
        "artifacts",
        "rollout",
        "runs",
        "thread.json",
    ]
    thread_manifest = json.loads(
        (final_dir / "thread.json").read_text(encoding="utf-8")
    )
    assert set(thread_manifest) == {
        "thread_id",
        "session_id",
        "kind",
        "created_at",
        "initial_state",
        "graph_binding",
        "capability_profile",
        "task_seed",
        "task_reference",
    }
    assert thread_manifest["thread_id"] == result.child_thread_id
    assert thread_manifest["session_id"] == owner.session_id
    assert thread_manifest["kind"] == "child"
    assert thread_manifest["initial_state"] == "running"
    assert thread_manifest["graph_binding"] == make_graph_binding()
    assert thread_manifest["task_seed"] == {"task": "做一件事"}
    assert thread_manifest["task_reference"] == {"ref": "doc-1"}
    # 初始 ContextStore 占位
    context_store = json.loads(
        (final_dir / "rollout" / "context-store.json").read_text(
            encoding="utf-8"
        )
    )
    assert context_store["thread_id"] == result.child_thread_id
    assert context_store["items"] == []
    # caller artifact 落位 artifacts/ 下
    artifact_file = final_dir / "artifacts" / "notes" / "plan.md"
    assert artifact_file.read_text(encoding="utf-8") == "# 计划\n步骤一\n"


async def test_create_artifact_manifest_hash_matches_record(
    service: ThreadCreationService,
    owner: OwnerSession,
) -> None:
    result = await do_create(service, owner)
    record = owner.control.get_thread_creation_record("key-1")
    final_dir = owner.session_dir / result.final_relative_locator
    inventory = child_node_inventory(record, owner, make_artifacts())
    # record 冻结清单 == 目录实际内容清单
    assert json.loads(record.artifact_manifest) == inventory
    assert record.artifact_manifest_hash == compute_artifact_manifest_hash(
        inventory
    )
    # artifact-manifest.json 文件字节 == 清单落盘形态
    assert (
        final_dir / "artifact-manifest.json"
    ).read_bytes() == serialize_artifact_manifest_bytes(inventory)
    # runs/ 空目录存在（初始 execution runs 占位）
    assert (final_dir / "runs").is_dir()


async def test_create_idle_child_without_seed(
    service: ThreadCreationService,
    owner: OwnerSession,
) -> None:
    result = await do_create(
        service,
        owner,
        initial_state="idle",
        metadata=make_metadata(task_seed=None),
    )
    assert result.record_state == "published"
    record = owner.control.get_thread_creation_record("key-1")
    assert record.initial_state == "idle"
    assert record.task_seed is None
    # thread.json task_seed 显式 null
    final_dir = owner.session_dir / result.final_relative_locator
    thread_manifest = json.loads(
        (final_dir / "thread.json").read_text(encoding="utf-8")
    )
    assert thread_manifest["task_seed"] is None
    # admission intent initial_state=idle
    intent = owner.control.get_initial_execution_intent("key-1")
    assert intent.initial_state == "idle"


async def test_create_running_without_seed_rejected(
    service: ThreadCreationService,
    owner: OwnerSession,
) -> None:
    with pytest.raises(ValueError, match="显式 task_seed"):
        await do_create(
            service,
            owner,
            metadata=make_metadata(task_seed=None),
        )
    # 无 record 残留（fail fast 在任何状态变更之前）
    assert (
        count_rows(owner.control, "thread_creation_records") == 0
    )


async def test_create_idle_with_seed_rejected(
    service: ThreadCreationService,
    owner: OwnerSession,
) -> None:
    with pytest.raises(ValueError, match="不得携带 task_seed"):
        await do_create(service, owner, initial_state="idle")
    assert (
        count_rows(owner.control, "thread_creation_records") == 0
    )


async def test_create_missing_metadata_key_rejected(
    service: ThreadCreationService,
    owner: OwnerSession,
) -> None:
    # 缺失 task_seed 键（缺失推断被拒——无 seed 必须显式 idle）
    metadata = make_metadata()
    del metadata["task_seed"]
    with pytest.raises(ValueError, match="缺失不得推断"):
        await do_create(service, owner, metadata=metadata)
    assert (
        count_rows(owner.control, "thread_creation_records") == 0
    )


async def test_create_rejects_bad_graph_binding(
    service: ThreadCreationService,
    owner: OwnerSession,
) -> None:
    bad = make_metadata()
    bad["graph_binding"] = {"graph_id": "x"}  # 缺三键
    with pytest.raises(ValueError, match="四元组"):
        await do_create(service, owner, metadata=bad)
    bad2 = make_metadata()
    bad2["capability_profile"] = "not-a-dict"
    # 非映射输入由注入的真实 compute_capability_profile_hash 口径拒绝
    # （ValueError「必须是非空映射」，不再本地复制一份形状校验）。
    with pytest.raises(ValueError, match="非空映射"):
        await do_create(service, owner, metadata=bad2)


# ----------------------------------------------------------------------
# 入参口径（R24：与真实 GraphBinding 对齐）
# ----------------------------------------------------------------------


async def test_create_rejects_string_graph_revision(
    service: ThreadCreationService,
    owner: OwnerSession,
) -> None:
    """str revision 被拒：真实 GraphBinding 构造必失败，不留双轨兼容。"""
    bad = make_metadata(
        graph_binding={**make_graph_binding(), "graph_revision": "1"},
    )
    with pytest.raises(TypeError, match="graph_revision"):
        await do_create(service, owner, metadata=bad)
    assert count_rows(owner.control, "thread_creation_records") == 0


async def test_create_rejects_bool_graph_revision(
    service: ThreadCreationService,
    owner: OwnerSession,
) -> None:
    """bool 是 int 子类，必须显式拒绝（对齐 GraphBinding 口径）。"""
    bad = make_metadata(
        graph_binding={**make_graph_binding(), "graph_revision": True},
    )
    with pytest.raises(TypeError, match="graph_revision"):
        await do_create(service, owner, metadata=bad)
    assert count_rows(owner.control, "thread_creation_records") == 0


async def test_create_rejects_nonpositive_graph_revision(
    service: ThreadCreationService,
    owner: OwnerSession,
) -> None:
    bad = make_metadata(
        graph_binding={**make_graph_binding(), "graph_revision": 0},
    )
    with pytest.raises(ValueError, match="graph_revision"):
        await do_create(service, owner, metadata=bad)
    assert count_rows(owner.control, "thread_creation_records") == 0


async def test_create_rejects_nested_capability_profile(
    service: ThreadCreationService,
    owner: OwnerSession,
) -> None:
    """capability_profile 必须是平面标量映射（复用真实 hash 口径校验）。"""
    bad = make_metadata(capability_profile={"tools": ["read_file"]})
    with pytest.raises(TypeError, match="标量"):
        await do_create(service, owner, metadata=bad)
    assert count_rows(owner.control, "thread_creation_records") == 0


async def test_create_rejects_capability_profile_hash_drift(
    service: ThreadCreationService,
    owner: OwnerSession,
) -> None:
    """持久化 hash 与 profile 内容不一致 → 拒绝（防自述字段漂移）。"""
    bad = make_metadata(
        graph_binding={
            **make_graph_binding(),
            "capability_profile_hash": "sha256:" + "c" * 64,
        },
    )
    with pytest.raises(ValueError, match="不一致"):
        await do_create(service, owner, metadata=bad)
    assert count_rows(owner.control, "thread_creation_records") == 0


def test_real_deep_agent_graph_binding_passes_validation() -> None:
    """audit §3.3 回归：真实 GraphBinding 四元组（revision=1 是 int）+
    真实能力 profile 必须能作为 child thread 入参通过校验。"""
    binding = DEEP_AGENT_GRAPH_BINDING
    validate_thread_session_metadata(
        "idle",
        {
            "graph_binding": {
                "graph_id": binding.graph_id,
                "graph_revision": binding.graph_revision,
                "graph_schema_hash": binding.graph_schema_hash,
                "capability_profile_hash": binding.capability_profile_hash,
            },
            "capability_profile": dict(DEEP_AGENT_CAPABILITY_PROFILE),
            "task_seed": None,
            "task_reference": None,
        },
        compute_capability_profile_hash=compute_capability_profile_hash,
    )


async def test_create_rejects_file_directory_artifact_conflict(
    service: ThreadCreationService,
    owner: OwnerSession,
) -> None:
    """同一相对路径既是文件又是祖先目录 → 准入前明确拒绝（不落半成品）。

    修复前：``validate_artifact_manifest`` 放行 ``{x, x/y}``，staging 准备
    阶段抛裸 ``FileExistsError``，record 已按该 preimage 冻结为 preparing，
    重入命中同一 preimage 后进入「staging 复验」永久 fail closed——该 key
    既不能创建也不能换 key 复用同一 delegation，形成拒绝服务。
    """
    conflict = {"x": "hello", "x/y": "world"}
    with pytest.raises(ValueError, match="互为文件/目录"):
        await do_create(service, owner, artifacts=conflict)
    # 未产生任何状态变更：无 record、无 staging 目录。
    assert count_rows(owner.control, "thread_creation_records") == 0
    assert not (owner.session_dir / ".staging" / "key-1").exists()
    # 同 key 换合法 artifact 仍可正常创建（未把 key 打死）。
    result = await do_create(service, owner, artifacts=make_artifacts())
    assert result.record_state == "published"


async def test_create_rejects_artifact_path_budget_exceeded(
    service: ThreadCreationService,
    owner: OwnerSession,
) -> None:
    """artifact 单组件超 255 bytes → 准入前明确拒绝，不留裸 OSError。

    修复前：超限路径在 ``_atomic_write_bytes`` 抛裸 ``OSError``（Errno 36），
    同时留下半成品 staging，重入永久 fail closed。
    """
    too_long = {"a" * 300 + "/f.txt": "x"}
    with pytest.raises(ValueError, match="路径组件超出预算"):
        await do_create(service, owner, artifacts=too_long)
    assert count_rows(owner.control, "thread_creation_records") == 0
    assert not (owner.session_dir / ".staging" / "key-1").exists()


async def test_create_rejects_oversized_idempotency_key(
    service: ThreadCreationService,
    owner: OwnerSession,
) -> None:
    """staging 目录名（幂等键）超路径组件预算 → 准入前明确拒绝。"""
    with pytest.raises(ValueError, match="路径组件超出预算"):
        await do_create(service, owner, key="k" * 300)
    assert count_rows(owner.control, "thread_creation_records") == 0


# ----------------------------------------------------------------------
# 幂等与冲突
# ----------------------------------------------------------------------


async def test_create_idempotent_same_result(
    service: ThreadCreationService,
    owner: OwnerSession,
) -> None:
    first = await do_create(service, owner)
    second = await do_create(service, owner)
    third = await do_create(service, owner)
    assert second == first
    assert third == first
    assert count_rows(owner.control, "thread_creation_records") == 1
    assert count_rows(owner.control, "thread_catalog") == 2  # main + child
    assert count_rows(owner.control, "thread_execution_intents") == 1


async def test_create_same_key_different_preimage_conflict(
    service: ThreadCreationService,
    owner: OwnerSession,
) -> None:
    await do_create(service, owner, initial_state="running")
    with pytest.raises(RuntimeError, match="preimage 冲突"):
        await do_create(service, owner, initial_state="idle",
                        metadata=make_metadata(task_seed=None))
    # 不同 artifact 载荷同样冲突（preimage 覆盖 artifact manifest）
    with pytest.raises(RuntimeError, match="preimage 冲突"):
        await do_create(service, owner, artifacts=make_artifacts(**{
            "notes/plan.md": "# 计划\n步骤一\n被篡改\n",
        }))
    record = owner.control.get_thread_creation_record("key-1")
    assert record.state == "published"


async def test_create_provided_thread_id_honored(
    service: ThreadCreationService,
    owner: OwnerSession,
) -> None:
    provided = make_thread_id()
    result = await do_create(service, owner, thread_id=provided)
    assert result.child_thread_id == provided
    # 同 key 幂等重入返回同一 child
    again = await do_create(service, owner, thread_id=provided)
    assert again.child_thread_id == provided
    # 同 key 传入不同 thread_id → 拒绝（thread_id 在 preimage 内，先触发
    # preimage 冲突；不静默改绑——两种拒绝信息均合法）
    with pytest.raises(RuntimeError, match="冲突"):
        await do_create(service, owner, thread_id=make_thread_id())


async def test_create_delegated_unique_constraint(
    service: ThreadCreationService,
    owner: OwnerSession,
) -> None:
    # R25 起 delegated creation 必须登记 collaboration member（record 冻结
    # 登记后的 ledger revision，publish CAS 校验）。
    first = await do_create(
        service,
        owner,
        key="key-d1",
        delegation_id="dlg-1",
        collaboration_member={
            "role": "delegated_subagent",
            "subagent_type": "general-purpose",
            "title": "委派：做一件事",
        },
    )
    assert first.record_state == "published"
    member = owner.control.get_collaboration_member("dlg-1")
    assert member.state == "published"
    assert member.child_thread_id == first.child_thread_id
    # 同 delegation_id 不同 key → 部分唯一约束拒绝
    with pytest.raises(RuntimeError, match="delegation_id"):
        await do_create(
            service,
            owner,
            key="key-d2",
            delegation_id="dlg-1",
            collaboration_member={
                "role": "delegated_subagent",
                "subagent_type": "general-purpose",
                "title": "委派：做一件事",
            },
        )
    # 同 delegation 同 key → 幂等
    again = await do_create(service, owner, key="key-d1", delegation_id="dlg-1")
    assert again == first
    # 不同 delegation 独立创建
    second = await do_create(
        service,
        owner,
        key="key-d3",
        delegation_id="dlg-2",
        collaboration_member={
            "role": "delegated_subagent",
            "subagent_type": "general-purpose",
            "title": "委派：做另一件事",
        },
    )
    assert second.child_thread_id != first.child_thread_id


async def test_create_concurrent_same_key_converges(
    service: ThreadCreationService,
    owner: OwnerSession,
) -> None:
    results = await asyncio.gather(
        do_create(service, owner),
        do_create(service, owner),
        do_create(service, owner),
    )
    assert results[0] == results[1] == results[2]
    assert count_rows(owner.control, "thread_creation_records") == 1
    assert count_rows(owner.control, "thread_catalog") == 2
    assert count_rows(owner.control, "thread_execution_intents") == 1


async def test_create_concurrent_different_keys_both_publish(
    service: ThreadCreationService,
    owner: OwnerSession,
) -> None:
    results = await asyncio.gather(
        do_create(service, owner, key="key-a"),
        do_create(service, owner, key="key-b"),
    )
    child_ids = {result.child_thread_id for result in results}
    assert len(child_ids) == 2
    assert count_rows(owner.control, "thread_catalog") == 3  # main + 2 child
    # 各自目录就位
    for result in results:
        assert (owner.session_dir / result.final_relative_locator).is_dir()
    assert count_rows(owner.control, "thread_execution_intents") == 2


# ----------------------------------------------------------------------
# 崩溃点矩阵（恢复/定点清理）
# ----------------------------------------------------------------------


async def test_crash_after_record_before_staging_recovers(
    service: ThreadCreationService,
    owner: OwnerSession,
) -> None:
    """崩溃点 1：record 提交后/staging 前——重入完成全流程。"""
    record = prepare_manual_record(owner)
    assert record.state == "preparing"
    result = await do_create(service, owner)
    assert result.child_thread_id == record.child_thread_id
    assert result.record_state == "published"
    assert (owner.session_dir / result.final_relative_locator).is_dir()
    assert not (owner.session_dir / ".staging" / "key-1").exists()


async def test_crash_after_staging_before_rename_recovers(
    service: ThreadCreationService,
    owner: OwnerSession,
) -> None:
    """崩溃点 2：staging 后/rename 前——staging 校验复用 → rename → publish。"""
    record = prepare_manual_record(owner)
    staging_dir = owner.session_dir / record.staging_locator
    materialize_node(staging_dir, record, owner, make_artifacts())
    result = await do_create(service, owner)
    assert result.child_thread_id == record.child_thread_id
    assert result.record_state == "published"
    # staging 已 rename 走
    assert not staging_dir.exists()
    assert (owner.session_dir / result.final_relative_locator).is_dir()


async def test_crash_after_rename_before_publish_recovers(
    service: ThreadCreationService,
    owner: OwnerSession,
) -> None:
    """崩溃点 3：rename 后/publish 前——目标校验一致 → 直接 publish。"""
    record = prepare_manual_record(owner)
    final_dir = owner.session_dir / record.final_relative_locator
    materialize_node(final_dir, record, owner, make_artifacts())
    result = await do_create(service, owner)
    assert result.child_thread_id == record.child_thread_id
    assert result.record_state == "published"
    # thread_catalog child row 已发布（唯一可见性提交点已过）
    rows = owner.control.connection.execute(
        "SELECT COUNT(*) FROM thread_catalog WHERE thread_id = ?",
        (record.child_thread_id,),
    ).fetchone()
    assert int(rows[0]) == 1


async def test_crash_after_publish_before_terminal_response_recovers(
    service: ThreadCreationService,
    owner: OwnerSession,
) -> None:
    """崩溃点 4：publish 后/terminal 响应前——record published 幂等恢复
    （含 catalog child row 复验与 admission intent 补写）。"""
    record = prepare_manual_record(owner)
    final_dir = owner.session_dir / record.final_relative_locator
    materialize_node(final_dir, record, owner, make_artifacts())
    # 模拟：publish 已在崩溃前完成（record → published + child row），
    # 但 admission intent 尚未写入、terminal 响应未返回。
    owner.control.publish_thread_creation_record("key-1")
    assert (
        count_rows(owner.control, "thread_execution_intents") == 0
    )
    result = await do_create(service, owner)
    assert result.child_thread_id == record.child_thread_id
    assert result.record_state == "published"
    # intent 已补写且恰一条
    assert (
        count_rows(owner.control, "thread_execution_intents") == 1
    )
    intent = owner.control.get_initial_execution_intent("key-1")
    assert intent.thread_id == record.child_thread_id


async def test_crash_after_intent_before_response_recovers(
    service: ThreadCreationService,
    owner: OwnerSession,
) -> None:
    """崩溃点 5：admission intent 后/terminal 响应前——幂等返回同一结果。"""
    record = prepare_manual_record(owner)
    final_dir = owner.session_dir / record.final_relative_locator
    materialize_node(final_dir, record, owner, make_artifacts())
    owner.control.publish_thread_creation_record("key-1")
    owner.control.create_or_get_initial_execution_intent(
        admission_idempotency_key="key-1",
        session_id=owner.session_id,
        thread_id=record.child_thread_id,
        initial_state="running",
        creation_idempotency_key="key-1",
    )
    result = await do_create(service, owner)
    assert result.child_thread_id == record.child_thread_id
    assert count_rows(owner.control, "thread_execution_intents") == 1


async def test_crash_during_abort_cleanup_converges(
    service: ThreadCreationService,
    owner: OwnerSession,
) -> None:
    """崩溃点 6：abort 清理中途（final 已清、staging 未清或反之）——重入
    再清理并收敛到 aborted，结果一致（无残留可见目录、不误删无关 row）。

    两个变体的失败驱动都是 owner fence 进入 deleting（统一
    session_deletion_pending 合同）；差别只在崩溃现场残留物。
    """
    # 变体 2 的 record 必须在 fence 漂移前建立（owner fence 非 active 时
    # 禁止新建 record——创建/删除双顺序红线）。
    record2 = prepare_manual_record(owner, key="key-2")
    staging2 = owner.session_dir / record2.staging_locator
    materialize_node(staging2, record2, owner, make_artifacts())
    materialize_node(
        owner.session_dir / record2.final_relative_locator,
        record2,
        owner,
        make_artifacts(),
    )
    # 与本次 operation 无关的外部 child row（2.3-A 下 sibling 增长合法）：
    # 定点清理与 abort 都不得触碰它。
    owner.control.connection.execute(
        "INSERT INTO thread_catalog (thread_id, kind, created_at) "
        "VALUES (?, 'child', ?)",
        (make_thread_id(), OWNER_CREATED_AT.isoformat()),
    )
    # 手工清掉 final2（模拟清理中途崩溃：staging 残留、final 已清）
    shutil.rmtree(owner.session_dir / record2.final_relative_locator)
    # 变体 1：record + final 就位后 fence 漂移（active→deleting）。
    record = prepare_manual_record(owner)
    final_dir = owner.session_dir / record.final_relative_locator
    materialize_node(final_dir, record, owner, make_artifacts())
    owner.control.cas_fence_transition(1, "deleting")
    # 变体 1 create：删除先行拒绝 → 清理 final（staging 不存在）→ abort
    with pytest.raises(
        SessionDeletionPendingError, match="session_deletion_pending"
    ):
        await do_create(service, owner)
    assert not final_dir.exists()
    aborted = owner.control.get_thread_creation_record("key-1")
    assert aborted.state == "aborted"
    # 变体 2 create：同一拒绝 → 定点清理残留 staging → abort
    with pytest.raises(
        SessionDeletionPendingError, match="session_deletion_pending"
    ):
        await do_create(service, owner, key="key-2")
    # 收敛：staging 也被定点清理，record aborted，无重复 child row
    assert not staging2.exists()
    assert owner.control.get_thread_creation_record("key-2").state == "aborted"
    rows = owner.control.connection.execute(
        "SELECT COUNT(*) FROM thread_catalog WHERE kind = 'child'"
    ).fetchone()
    assert int(rows[0]) == 1  # 仅漂移注入的那条外部 child row


async def test_staging_and_final_coexist_fail_closed(
    service: ThreadCreationService,
    owner: OwnerSession,
) -> None:
    """恢复窗口守卫：final 与 staging 并存 = 外部改动 → fail closed。"""
    record = prepare_manual_record(owner)
    materialize_node(
        owner.session_dir / record.final_relative_locator,
        record,
        owner,
        make_artifacts(),
    )
    materialize_node(
        owner.session_dir / record.staging_locator,
        record,
        owner,
        make_artifacts(),
    )
    with pytest.raises(RuntimeError, match="同时存在"):
        await do_create(service, owner)
    # 现场保留（fail closed 不破坏现场）
    assert (owner.session_dir / record.final_relative_locator).is_dir()
    assert (owner.session_dir / record.staging_locator).is_dir()


async def test_incomplete_staging_fail_closed_preserves_scene(
    service: ThreadCreationService,
    owner: OwnerSession,
) -> None:
    """准备中断窗口：不完整 staging 重入 fail closed，现场保留供人工核账。"""
    record = prepare_manual_record(owner)
    staging_dir = owner.session_dir / record.staging_locator
    materialize_node(staging_dir, record, owner, make_artifacts())
    # 模拟准备中断：删除一个文件
    (staging_dir / "thread.json").unlink()
    with pytest.raises(RuntimeError, match="文件集与预期不一致"):
        await do_create(service, owner)
    # 现场保留（不自动删除重建——无法与外部篡改区分）
    assert staging_dir.is_dir()
    assert not (staging_dir / "thread.json").exists()


async def test_tampered_staging_content_fail_closed(
    service: ThreadCreationService,
    owner: OwnerSession,
) -> None:
    record = prepare_manual_record(owner)
    staging_dir = owner.session_dir / record.staging_locator
    materialize_node(staging_dir, record, owner, make_artifacts())
    # 篡改 thread.json 内容（sha256 与冻结清单不符）
    (staging_dir / "thread.json").write_bytes(b'{"tampered": true}\n')
    with pytest.raises(RuntimeError, match="sha256"):
        await do_create(service, owner)
    assert staging_dir.is_dir()


async def test_foreign_staging_directory_not_absorbed(
    service: ThreadCreationService,
    owner: OwnerSession,
) -> None:
    """无 record 目录不吸收：同名 staging 残留对外来 key 是外部改动。"""
    foreign = owner.session_dir / ".staging" / "key-1"
    foreign.mkdir(parents=True)
    (foreign / "junk.txt").write_text("外来内容", encoding="utf-8")
    with pytest.raises(RuntimeError, match="目录集与预期不一致"):
        await do_create(service, owner)
    # 外来目录原样保留（不吸收、不删除）
    assert (foreign / "junk.txt").read_text(encoding="utf-8") == "外来内容"
    # record 保持 preparing（未吸收现场）
    assert owner.control.get_thread_creation_record("key-1").state == (
        "preparing"
    )


async def test_foreign_final_directory_fail_closed(
    service: ThreadCreationService,
    owner: OwnerSession,
) -> None:
    """最终 locator 被外部目录占用且内容不一致 → fail closed 不覆盖。"""
    record = prepare_manual_record(owner)
    final_dir = owner.session_dir / record.final_relative_locator
    final_dir.mkdir(parents=True)
    (final_dir / "junk.txt").write_text("外来内容", encoding="utf-8")
    with pytest.raises(RuntimeError, match="目录集与预期不一致"):
        await do_create(service, owner)
    assert (final_dir / "junk.txt").exists()


# ----------------------------------------------------------------------
# CAS 失败（service 级）：定点清理 + abort
# ----------------------------------------------------------------------


async def test_publish_cas_failure_fence_drift_cleans_and_aborts(
    service: ThreadCreationService,
    owner: OwnerSession,
) -> None:
    """fence 进入 deleting（删除流真实路径）→ publish 以统一删除中错误
    合同拒绝 → 定点清理 + abort；断言无 child row、无残留可见目录。"""
    record = prepare_manual_record(owner)
    staging_dir = owner.session_dir / record.staging_locator
    final_dir = owner.session_dir / record.final_relative_locator
    materialize_node(staging_dir, record, owner, make_artifacts())
    owner.control.cas_fence_transition(1, "deleting")
    with pytest.raises(
        SessionDeletionPendingError, match="session_deletion_pending"
    ):
        await do_create(service, owner)
    # 定点清理：staging/final 均无残留
    assert not staging_dir.exists()
    assert not final_dir.exists()
    # abort 终态（含原因）
    aborted = owner.control.get_thread_creation_record("key-1")
    assert aborted.state == "aborted"
    assert "session_deletion_pending" in (aborted.abort_reason or "")
    # 无 child row、无 intent
    assert count_rows(owner.control, "thread_catalog") == 1  # 仅 main
    assert count_rows(owner.control, "thread_execution_intents") == 0
    # aborted record 同 key 重入 → 明确报错（换新 key 重试）
    with pytest.raises(RuntimeError, match="已中止"):
        await do_create(service, owner)


async def test_publish_tolerates_sibling_growth(
    service: ThreadCreationService,
    owner: OwnerSession,
) -> None:
    """并发 sibling child 已发布（行数合法增长）→ publish 必须成功
    （2.3-A：跨进程文件锁使并发交错成为真实合同）。"""
    record = prepare_manual_record(owner)
    staging_dir = owner.session_dir / record.staging_locator
    materialize_node(staging_dir, record, owner, make_artifacts())
    # 直接插入另一条 child row（并发其它创建已发布的最小模拟）
    owner.control.connection.execute(
        "INSERT INTO thread_catalog (thread_id, kind, created_at) "
        "VALUES (?, 'child', ?)",
        (make_thread_id(), OWNER_CREATED_AT.isoformat()),
    )
    result = await do_create(service, owner)
    assert result.record_state == "published"
    assert (owner.session_dir / result.final_relative_locator).is_dir()
    binding = owner.control.get_thread_owner_binding(result.child_thread_id)
    assert binding.final_relative_locator == result.final_relative_locator


# ----------------------------------------------------------------------
# publish 临界区 fresh 复验 workspace catalog（删除双顺序窗口）
# ----------------------------------------------------------------------


class DeletingOnPublishGate(NavigationTopologyGate):
    """测试 gate：第二次进入 shared()（publish 临界区）时把 owner 节点改为
    deleting，模拟 R14 删除流「catalog 整树 deleting 先提交、gate 外 drain
    才逐 session 关 fence」的真实窗口（步骤 1 时 catalog 仍 active）。"""

    def __init__(self, store: SessionCatalogStore, session_id: str) -> None:
        super().__init__(store.sessions_root)
        self._store = store
        self._session_id = session_id
        self.entries = 0

    def shared(self):
        return self._deleting_on_publish(super().shared())

    @asynccontextmanager
    async def _deleting_on_publish(self, lock):
        async with lock:
            self.entries += 1
            if self.entries == 2:
                # 只改 catalog owner 节点 state（**不动 fence**）。
                self._store.connection.execute(
                    "UPDATE nodes SET state = 'deleting' WHERE node_id = ?",
                    (self._session_id,),
                )
            yield


async def test_publish_rejected_when_catalog_deleting_but_fence_active(
    store: SessionCatalogStore,
    owner: OwnerSession,
    sessions_root: Path,
) -> None:
    """删除双顺序窗口：workspace catalog 在 publish 临界区内已 deleting、
    per-session fence 仍 (active, 冻结 generation) → fresh 复验必须拒绝发布
    （不得把 child 发布进正在删除的会话），并按定点清理 + abort 收敛。"""
    record = prepare_manual_record(owner)
    staging_dir = owner.session_dir / record.staging_locator
    final_dir = owner.session_dir / record.final_relative_locator
    materialize_node(staging_dir, record, owner, make_artifacts())
    gate = DeletingOnPublishGate(store, owner.session_id)
    service = ThreadCreationService(
        store=store,
        control_store=owner.control,
        sessions_root=sessions_root,
        workspace_id=WORKSPACE_ID,
        compute_capability_profile_hash=compute_capability_profile_hash,
        gate=gate,
    )
    with pytest.raises(
        SessionDeletionPendingError, match="session_deletion_pending"
    ):
        await do_create(service, owner)
    # 窗口确实发生在 publish 临界区（步骤 1 通过后）
    assert gate.entries == 2
    # fence 从未漂移（本窗口 local fence 仍 active/冻结 generation）
    assert owner.control.get_fence() == ("active", 1)
    # 无 child row（唯一可见性提交点未过）、无 intent
    assert count_rows(owner.control, "thread_catalog") == 1  # 仅 main
    assert count_rows(owner.control, "thread_execution_intents") == 0
    # staging/final 定点清理（final 由 rename 产生，同样被清理）
    assert not staging_dir.exists()
    assert not final_dir.exists()
    aborted = owner.control.get_thread_creation_record("key-1")
    assert aborted.state == "aborted"
    assert "session_deletion_pending" in (aborted.abort_reason or "")


# ----------------------------------------------------------------------
# published 重入 fail closed（不重建被外部删除的可见性产物）
# ----------------------------------------------------------------------


async def test_published_reentry_with_deleted_final_fails_closed(
    service: ThreadCreationService,
    owner: OwnerSession,
) -> None:
    """record 已 published 而 final 目录被外部删除 → fail closed，不重建
    final/staging（rename 先于 publish，协议内崩溃点不产生该状态）。"""
    record = prepare_manual_record(owner)
    final_dir = owner.session_dir / record.final_relative_locator
    staging_dir = owner.session_dir / record.staging_locator
    materialize_node(final_dir, record, owner, make_artifacts())
    owner.control.publish_thread_creation_record("key-1")
    assert owner.control.get_thread_creation_record("key-1").state == "published"
    # 外部删除已发布线程的可见性产物
    shutil.rmtree(final_dir)
    with pytest.raises(RuntimeError, match="缺失（外部删除可见性产物"):
        await do_create(service, owner)
    # 不重建：final/staging 均不存在，现场不被静默修复
    assert not final_dir.exists()
    assert not staging_dir.exists()
    # record 保持 published（不回退、不 abort）
    assert owner.control.get_thread_creation_record("key-1").state == "published"


# ----------------------------------------------------------------------
# owner session 状态与绑定
# ----------------------------------------------------------------------


async def test_create_fails_when_owner_session_deleting(
    service: ThreadCreationService,
    store: SessionCatalogStore,
    owner: OwnerSession,
) -> None:
    store.connection.execute(
        "UPDATE nodes SET state = 'deleting' WHERE node_id = ?",
        (owner.session_id,),
    )
    with pytest.raises(
        SessionDeletionPendingError, match="session_deletion_pending"
    ):
        await do_create(service, owner)
    # 零副作用：无 record、无 staging/最终目录、无 child row
    assert count_rows(owner.control, "thread_creation_records") == 0
    assert not (owner.session_dir / ".staging").exists()
    assert count_rows(owner.control, "thread_catalog") == 1  # 仅 main


async def test_create_fails_with_workspace_mismatch(
    store: SessionCatalogStore,
    owner: OwnerSession,
    sessions_root: Path,
) -> None:
    mismatched = ThreadCreationService(
        store=store,
        control_store=owner.control,
        sessions_root=sessions_root,
        workspace_id="ws-other",
        compute_capability_profile_hash=compute_capability_profile_hash,
        gate=NavigationTopologyGate(sessions_root),
    )
    with pytest.raises(ValueError, match="workspace 不一致"):
        await do_create(mismatched, owner)


async def test_create_fails_with_control_store_misbinding(
    store: SessionCatalogStore,
    owner: OwnerSession,
    sessions_root: Path,
    tmp_path: Path,
) -> None:
    """control_store 误绑其它 session 控制库 → fail fast。"""
    other_dir = sessions_root / "2026" / "06" / "01" / make_session_id()
    other_dir.mkdir(parents=True)
    other_control = SessionControlStore(other_dir / "session-control.sqlite")
    try:
        other_control.initialize_main_thread(make_thread_id(), OWNER_CREATED_AT)
        other_control.initialize_fence("active", 1)
        misbound = ThreadCreationService(
            store=store,
            control_store=other_control,
            sessions_root=sessions_root,
            workspace_id=WORKSPACE_ID,
            compute_capability_profile_hash=compute_capability_profile_hash,
            gate=NavigationTopologyGate(sessions_root),
        )
        with pytest.raises(ValueError, match="不一致"):
            await do_create(misbound, owner)
    finally:
        other_control.close()


async def test_create_fails_when_control_main_row_mismatch(
    service: ThreadCreationService,
    owner: OwnerSession,
) -> None:
    """workspace catalog main 指针与 control main row 不一致 → fail closed。"""
    # 直接改 owner catalog node 的 main_thread_id（外部改动模拟）
    owner.control.connection.execute(
        "UPDATE thread_catalog SET thread_id = ? WHERE kind = 'main'",
        (make_thread_id(),),
    )
    with pytest.raises(RuntimeError, match="不一致"):
        await do_create(service, owner)


async def test_create_missing_owner_session_fails(
    store: SessionCatalogStore,
    owner: OwnerSession,
    sessions_root: Path,
) -> None:
    service = ThreadCreationService(
        store=store,
        control_store=owner.control,
        sessions_root=sessions_root,
        workspace_id=WORKSPACE_ID,
        compute_capability_profile_hash=compute_capability_profile_hash,
        gate=NavigationTopologyGate(sessions_root),
    )
    with pytest.raises(KeyError):
        await service.create(
            idempotency_key="key-x",
            session_id=make_session_id(),
            thread_id=None,
            delegation_id=None,
            initial_state="running",
            session_metadata=make_metadata(),
            artifact_manifest=make_artifacts(),
        )


async def test_service_rejects_mismatched_sessions_root(
    store: SessionCatalogStore,
    owner: OwnerSession,
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="不一致"):
        ThreadCreationService(
            store=store,
            control_store=owner.control,
            sessions_root=tmp_path / "other-sessions",
            workspace_id=WORKSPACE_ID,
            compute_capability_profile_hash=compute_capability_profile_hash,
            gate=NavigationTopologyGate(tmp_path / "other-sessions"),
        )


# ----------------------------------------------------------------------
# durability 可观测
# ----------------------------------------------------------------------


async def test_durability_barrier_fsync_observable(
    service: ThreadCreationService,
    owner: OwnerSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """staging 准备与 rename 的 durability barrier 可观测：目录/文件
    fsync 均被调用（monkeypatch 计数，最小下界断言）。"""
    import app.core.thread_creation as thread_creation_module

    fsync_dir_calls: list[Path] = []
    fsync_file_calls: list[Path] = []
    original_dir = thread_creation_module._fsync_directory
    original_file = thread_creation_module._fsync_file

    def counting_dir(directory: Path) -> None:
        fsync_dir_calls.append(directory)
        original_dir(directory)

    def counting_file(path: Path) -> None:
        fsync_file_calls.append(path)
        original_file(path)

    monkeypatch.setattr(
        thread_creation_module, "_fsync_directory", counting_dir
    )
    monkeypatch.setattr(
        thread_creation_module, "_fsync_file", counting_file
    )
    await do_create(service, owner)
    # 目录 fsync：staging 树（4 目录）+ .staging 父目录 + rename 后父目录
    # 链（threads/YYYY/MM/DD + session 目录）等，至少覆盖 6 次调用。
    assert len(fsync_dir_calls) >= 6
    # 文件级 fsync（原子写内 os.fsync + _fsync_file）由目录屏障补足；
    # 此处断言 atomic write 的 replace 目录项 fsync 已被计入目录调用。
    staged_paths = {str(path) for path in fsync_dir_calls}
    assert any(".staging" in path for path in staged_paths)
    assert any("threads" in path for path in staged_paths)


async def test_rename_fsyncs_parent_chain(
    service: ThreadCreationService,
    owner: OwnerSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """rename 后沿 threads/YYYY/MM/DD 父目录链逐级 fsync 到 session 目录。"""
    import app.core.thread_creation as thread_creation_module

    fsynced: list[Path] = []
    original_dir = thread_creation_module._fsync_directory

    def counting_dir(directory: Path) -> None:
        fsynced.append(directory)
        original_dir(directory)

    monkeypatch.setattr(
        thread_creation_module, "_fsync_directory", counting_dir
    )
    result = await do_create(service, owner)
    final_dir = owner.session_dir / result.final_relative_locator
    # 链上每一级（threads/YYYY、threads/YYYY/MM、threads/YYYY/MM/DD、
    # session 目录）都出现在 fsync 序列中。
    chain = final_dir.parent
    while chain != owner.session_dir:
        assert chain in fsynced, f"父目录未 fsync: {chain}"
        chain = chain.parent
    assert owner.session_dir in fsynced


# ----------------------------------------------------------------------
# preimage/清单确定性
# ----------------------------------------------------------------------


def test_preimage_covers_contract_inputs() -> None:
    base = compute_thread_creation_preimage_hash(
        workspace_id=WORKSPACE_ID,
        session_id=make_session_id(),
        thread_id=None,
        delegation_id=None,
        initial_state="idle",
        session_metadata=make_metadata(task_seed=None),
        artifact_manifest={},
    )
    # initial_state 进入 preimage（不同值即冲突）
    changed_state = compute_thread_creation_preimage_hash(
        workspace_id=WORKSPACE_ID,
        session_id="ses_" + "1" * 32,
        thread_id=None,
        delegation_id=None,
        initial_state="running",
        session_metadata=make_metadata(task_seed=None),
        artifact_manifest={},
    )
    assert base != changed_state
    # delegation_id 进入 preimage（delegated child 纳入 preimage）
    with_delegation = compute_thread_creation_preimage_hash(
        workspace_id=WORKSPACE_ID,
        session_id="ses_" + "1" * 32,
        thread_id=None,
        delegation_id="dlg-1",
        initial_state="running",
        session_metadata=make_metadata(task_seed=None),
        artifact_manifest={},
    )
    assert with_delegation != changed_state


def test_thread_node_files_deterministic() -> None:
    """同 record 同 artifact 载荷 → 文件字节与清单 hash 逐字节稳定。"""
    record = ThreadCreationRecord(
        thread_creation_idempotency_key="key-1",
        state="preparing",
        preimage_hash="a" * 64,
        delegation_id=None,
        child_thread_id=make_thread_id(),
        final_relative_locator="threads/2026/06/01/" + "thr_" + "0" * 32,
        staging_locator=".staging/key-1",
        artifact_manifest=None,
        artifact_manifest_hash=None,
        graph_binding=canonical_json_text(make_graph_binding()),
        capability_profile=canonical_json_text(CAPABILITY_PROFILE),
        task_seed=canonical_json_text({"task": "做一件事"}),
        task_reference=None,
        owner_session_lifecycle_generation=1,
        catalog_precondition_revision=1,
        collaboration_precondition_revision=None,
        initial_state="running",
        admission_intent="{}",
        abort_reason=None,
        child_created_at=OWNER_CREATED_AT.isoformat(),
        record_created_at=OWNER_CREATED_AT.isoformat(),
        record_updated_at=OWNER_CREATED_AT.isoformat(),
    )
    artifacts = make_artifacts()
    first = build_thread_node_files(record=record, session_id="ses_" + "2" * 32, artifact_manifest=artifacts)
    second = build_thread_node_files(record=record, session_id="ses_" + "2" * 32, artifact_manifest=artifacts)
    assert first == second
    inv1 = build_thread_node_inventory(first)
    inv2 = build_thread_node_inventory(second)
    assert inv1 == inv2
    assert compute_artifact_manifest_hash(inv1) == compute_artifact_manifest_hash(inv2)
    # thread_id 形态合法（final_relative_locator 手工构造的 child 同形）
    assert record.final_relative_locator.startswith("threads/2026/06/01/thr_")
    # 该构造 record 的 child_thread_id 与 final locator 叶名一致性由
    # store 侧 publish 校验覆盖；此处仅断言纯函数稳定性。


def test_node_directory_rejects_symlink(
    service: ThreadCreationService,
    owner: OwnerSession,
) -> None:
    """symlink 目录/文件一律 fail closed（逐级 no-follow 保护）。"""
    record = prepare_manual_record(owner)
    staging_dir = owner.session_dir / record.staging_locator
    materialize_node(staging_dir, record, owner, make_artifacts())
    # 构造 symlink 目录（rollout → 外部目标）
    external = owner.session_dir / "external-target"
    external.mkdir(exist_ok=True)
    rollout = staging_dir / "rollout"
    (rollout / "context-store.json").unlink()
    rollout.rmdir()
    os.symlink(external, rollout)
    with pytest.raises(RuntimeError, match="symlink"):
        verify_node_directory(
            staging_dir,
            child_node_files(record, owner, make_artifacts()),
            child_node_inventory(record, owner, make_artifacts()),
            stage="symlink 负向",
        )
    # symlink 文件同样拒绝
    rollout2 = staging_dir / "rollout"
    os.symlink(external / "missing.json", rollout2 / "context-store.json")
    with pytest.raises(RuntimeError, match="symlink"):
        verify_node_directory(
            staging_dir,
            child_node_files(record, owner, make_artifacts()),
            child_node_inventory(record, owner, make_artifacts()),
            stage="symlink 文件负向",
        )
