"""rollout 持久化定位的 thread-qualified 缺口 fail-closed 合同。

OpenSpec 8.3 的 (session_id, thread_id) thread-qualified 定位尚未落地：
rollout 物理节点（root() / index.sqlite / rollout.jsonl）与单行
database_meta（committed_jsonl_offset / source_overlay_epoch /
last_control_sequence）当前是 Session 级 singleton，只由 main thread 独占。
本测试锁定「拒绝把 thread_id 当作 session_id 静默解析」的 fail-closed 行为：
真实 canonical thr_ thread id 与裸 main 别名都必须显式报错，绝不能静默回退到
session 语义。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.services.infrastructure.rollout_context.storage.service import RolloutStorage
from tests.support.catalog_session_bundle import seed_catalog_session_bundle

SESSION_ID = "ses_1cb2d44643ae45818a69dc2c654c06c7"
THREAD_ID = "thr_bfc75d66aebc4b7984711000b05bc503"
MAIN_ALIAS = "main"


@pytest.fixture
def storage(tmp_path: Path) -> RolloutStorage:
    root = tmp_path / ".boxteam" / "sessions"
    root.mkdir(parents=True)
    seed_catalog_session_bundle(root, SESSION_ID, title="thread 定位 fail-closed")
    return RolloutStorage(root)


def test_root_accepts_main_session_id_and_locates_session_node(
    storage: RolloutStorage,
) -> None:
    session_node = storage._path_resolver.resolve_session_node(SESSION_ID)
    assert storage.root(SESSION_ID) == session_node / "rollout"
    assert storage.index_path(SESSION_ID) == session_node / "rollout" / "index.sqlite"
    assert storage.jsonl_path(SESSION_ID) == session_node / "rollout" / "rollout.jsonl"


@pytest.mark.parametrize("entry", ["root", "index_path", "jsonl_path"])
def test_path_entry_points_reject_canonical_thread_id(
    storage: RolloutStorage, entry: str,
) -> None:
    path_entry = getattr(storage, entry)
    with pytest.raises(RuntimeError, match="拒绝把 thread_id 当作 session_id"):
        path_entry(THREAD_ID)


@pytest.mark.parametrize("entry", ["root", "index_path", "jsonl_path"])
def test_path_entry_points_reject_main_alias(
    storage: RolloutStorage, entry: str,
) -> None:
    path_entry = getattr(storage, entry)
    with pytest.raises(RuntimeError, match="拒绝把 thread_id 当作 session_id"):
        path_entry(MAIN_ALIAS)


def test_root_rejects_empty_session_id(storage: RolloutStorage) -> None:
    with pytest.raises(TypeError, match="非空 session_id"):
        storage.root("")

