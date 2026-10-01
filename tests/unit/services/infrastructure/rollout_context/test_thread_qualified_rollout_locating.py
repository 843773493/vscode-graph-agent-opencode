"""rollout 持久化定位的 thread-qualified 合同（OpenSpec 8.3 主项）。

rollout 物理节点、index.sqlite 与 rollout.jsonl 由精确 ``(session_id, thread_id)``
的 thread node 独占：main thread 经 thread catalog 冻结的 ``main_thread_id`` 解析，
非 main durable thread 经其 frozen locator 解析到独立节点。裸 ``main`` 别名与裸
session_id 不是 thread identity，必须经 thread catalog 显式解析，绝不把它们当作
thread_id 拼路径；thread 维度缺省或非法时 fail closed。
"""

from __future__ import annotations

import hashlib
import inspect
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.core.identifier import create_uuid_hex
from app.core.session_control_store import SessionControlStore
from app.services.infrastructure.rollout_context.storage.service import RolloutStorage
from tests.support.catalog_session_bundle import (
    seed_catalog_session_bundle,
)

SESSION_ID = f"ses_{create_uuid_hex()}"
CHILD_THREAD_ID = f"thr_{create_uuid_hex()}"


def test_staging_owners_accept_thread_qualified_root_signature() -> None:
    """fork/migration 私有 staging owner 必须与基类 thread-qualified root 签名一致。

    回归：thread-qualified 化把 _lock 改成调用
    self.root(owner_session_id, checkpoint_ns, thread_id=owner_thread_id)，
    但两个 staging 子类仍保留旧签名 root(session_id, checkpoint_ns)。私有
    staging 根按 session 建立、owner_thread_id 恒为 None；子类不接受该 kwarg
    时，full_rollout_copy 与 legacy migration 在取锁处直接 TypeError。显式传入
    真实 non-main thread 时必须 fail closed，绝不把 thread 当 session 静默解析
    到同一个 staging 根。
    """
    import pathlib

    from app.services.infrastructure.rollout_context.fork.full_copy.staging import (
        FullCopyStagingStorage,
    )
    from app.services.infrastructure.rollout_context.migration.store import (
        _StagingStorage,
    )

    target = f"ses_{create_uuid_hex()}"
    for cls in (FullCopyStagingStorage, _StagingStorage):
        parameters = inspect.signature(cls.root).parameters
        assert "thread_id" in parameters, (
            f"{cls.__name__}.root 必须接受 thread_id kwarg，"
            "否则 _lock 的 thread-qualified 调用会 TypeError"
        )
        assert parameters["thread_id"].kind is inspect.Parameter.KEYWORD_ONLY
        assert parameters["thread_id"].default is None

    probe_root = pathlib.Path("/tmp/staging-root-signature-probe")
    for cls in (FullCopyStagingStorage, _StagingStorage):
        instance = object.__new__(cls)
        if cls is FullCopyStagingStorage:
            instance._source = f"ses_{create_uuid_hex()}"
            instance._target = target
            instance._stage_root = probe_root
        else:
            instance._target = target
            instance._root = probe_root
        assert instance.root(target, "", thread_id=None) == probe_root
        with pytest.raises(ValueError, match="不接受显式 thread_id"):
            instance.root(target, "", thread_id=CHILD_THREAD_ID)


def _publish_child_thread(
    session_dir: Path,
    *,
    thread_id: str,
    created_at: datetime,
) -> Path:
    """用 R20 store API 发布最小 child，再创建其受检物理目录。"""
    control = SessionControlStore(session_dir / "session-control.sqlite")
    key = f"rollout-locating-{thread_id}"
    try:
        control.create_or_get_thread_creation_record(
            idempotency_key=key,
            initial_state="idle",
            preimage_hash="a" * 64,
            graph_binding=json.dumps({"graph_id": "rollout-locating"}),
            capability_profile=json.dumps({}),
            created_at=created_at,
            thread_id=thread_id,
        )
        manifest = "{}"
        control.freeze_thread_creation_artifact_manifest(
            key,
            artifact_manifest=manifest,
            artifact_manifest_hash=hashlib.sha256(
                manifest.encode("utf-8")
            ).hexdigest(),
        )
        published = control.publish_thread_creation_record(key)
        locator = published.final_relative_locator
    finally:
        control.close()
    thread_dir = session_dir / locator
    thread_dir.mkdir(parents=True)
    return thread_dir


@pytest.fixture
def storage(tmp_path: Path) -> RolloutStorage:
    root = tmp_path / ".boxteam" / "sessions"
    root.mkdir(parents=True)
    seed_catalog_session_bundle(root, SESSION_ID, title="thread 定位")
    return RolloutStorage(root)


def test_main_thread_resolves_to_thread_node_rollout(
    storage: RolloutStorage,
) -> None:
    main_thread_id = storage._path_resolver.main_thread_id(SESSION_ID)
    session_node = storage._path_resolver.resolve_session_node(SESSION_ID)
    thread_node = storage._path_resolver.resolve_thread_node(
        SESSION_ID, main_thread_id
    )
    assert storage.root(SESSION_ID) == thread_node / "rollout"
    assert storage.root(SESSION_ID, thread_id=main_thread_id) == thread_node / "rollout"
    assert storage.index_path(SESSION_ID) == thread_node / "rollout" / "index.sqlite"
    assert storage.jsonl_path(SESSION_ID) == thread_node / "rollout" / "rollout.jsonl"
    # main 的 thread node 当前折叠到 session node（R3a 过渡形态）。
    assert thread_node == session_node


def test_child_thread_resolves_to_own_thread_node_rollout(
    storage: RolloutStorage,
) -> None:
    session_node = storage._path_resolver.resolve_session_node(SESSION_ID)
    thread_dir = _publish_child_thread(
        session_node,
        thread_id=CHILD_THREAD_ID,
        # locator 日期段必须等于 thread_id 内嵌 UUIDv7 UTC 日期，故用当前时刻。
        created_at=datetime.now(UTC),
    )
    child_root = storage.root(SESSION_ID, thread_id=CHILD_THREAD_ID)
    assert child_root == thread_dir / "rollout"
    assert child_root != session_node / "rollout"
    assert storage.index_path(SESSION_ID, thread_id=CHILD_THREAD_ID) == (
        thread_dir / "rollout" / "index.sqlite"
    )


def test_unpublished_thread_id_fails_closed(
    storage: RolloutStorage,
) -> None:
    with pytest.raises(KeyError):
        storage.root(SESSION_ID, thread_id=CHILD_THREAD_ID)


def test_unknown_session_fails_closed() -> None:
    with pytest.raises(TypeError, match="非空字符串"):
        RolloutStorage(Path("/tmp/does-not-exist")).root("")


def test_main_and_child_rollout_stores_are_physically_isolated(
    storage: RolloutStorage,
) -> None:
    """OpenSpec 8.3 Scenario：main 与 child 的 rollout 落点互不重叠。

    线程级隔离由「每 thread 独立数据库文件」实现：main 与 child 各自的
    `index.sqlite` 与 `rollout.jsonl` 落在不同物理目录，故两库各自的单行
    `database_meta` 与 `committed_jsonl_offset` 不会互相串扰。
    """
    session_node = storage._path_resolver.resolve_session_node(SESSION_ID)
    _publish_child_thread(
        session_node,
        thread_id=CHILD_THREAD_ID,
        created_at=datetime.now(UTC),
    )
    main_root = storage.root(SESSION_ID)
    child_root = storage.root(SESSION_ID, thread_id=CHILD_THREAD_ID)
    assert main_root != child_root
    assert main_root.parent != child_root.parent
    assert storage.index_path(SESSION_ID) != storage.index_path(
        SESSION_ID, thread_id=CHILD_THREAD_ID
    )
    assert storage.jsonl_path(SESSION_ID) != storage.jsonl_path(
        SESSION_ID, thread_id=CHILD_THREAD_ID
    )
