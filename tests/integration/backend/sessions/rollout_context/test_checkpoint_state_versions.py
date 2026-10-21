"""非 messages 状态版本的复用、碰撞、冷读和旧库迁移。"""

from __future__ import annotations

import hashlib
import sqlite3

import pytest
from langchain_core.messages import HumanMessage
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

from app.core.checkpoint_config import build_checkpoint_config
from app.domain.itemized.hashing import sha256_jcs
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from app.services.infrastructure.rollout_context.storage.catalog.history_scope import (
    HistoricalNamespaceBinding,
)
from app.services.infrastructure.rollout_context.storage.schema_channel_versions import (
    CHANNEL_VALUES_MIGRATION_SQL,
)
from tests.integration.backend.sessions.rollout_context._channel_version_fixtures import (
    restore_schema19,
)
from tests.integration.backend.sessions.rollout_context.test_rollout_checkpoint_saver import (
    SESSION_ID,
    _accept_user_execution,
    _checkpoint,
    _execution_scope,
)


class CountingSerializer(JsonPlusSerializer):
    def __init__(self):
        super().__init__()
        self.large_writes = 0

    def dumps_typed(self, obj):
        if isinstance(obj, dict) and "large_version_probe" in obj:
            self.large_writes += 1
        return super().dumps_typed(obj)


@pytest.fixture
def version_case(tmp_path, session_bundle_factory):
    sessions = tmp_path / "sessions"
    session_bundle_factory(sessions, SESSION_ID)
    serde = CountingSerializer()
    saver = RolloutCheckpointSaver(sessions, serde=serde)
    thread = saver.forks.source.resolve_main_thread_owner(SESSION_ID).thread_id
    user = HumanMessage(content="验证不可变状态版本", id="state-version-user")
    admission = _accept_user_execution(saver, SESSION_ID, thread, user, turn_id="state-version-turn")
    index = saver._storage.storage.root(SESSION_ID, thread_id=thread) / "index.sqlite"
    return saver, serde, admission, user, index


def _put(case, parent, name, value, *, version="v1", changed=False):
    saver, _serde, _admission, user, _index = case
    checkpoint = _checkpoint(name, [user], state=value)
    checkpoint["channel_versions"]["state"] = version
    return saver.put(parent, checkpoint, {"source": "unit"}, {"state": version} if changed else {})


@pytest.mark.parametrize("rich_value", [False, True])
def test_unchanged_version_has_one_body_and_one_serialization_after_reopen(version_case, rich_value):
    saver, serde, admission, _user, index = version_case
    value = {"large_version_probe": ["正文 Ω" * 1000], "optional": None}
    if rich_value:
        value["summary_message"] = HumanMessage(content="摘要正文 Ω" * 1000, id="state-summary")
    with _execution_scope(admission):
        root = _put(version_case, build_checkpoint_config(admission.thread_id), "version-root", value, changed=True)
        child = _put(version_case, root, "version-child", value)
        # 用新对象与冷 saver 继续；不能依赖对象地址或进程缓存。
        saver = RolloutCheckpointSaver(saver._storage.storage.sessions_dir, serde=serde)
        cold_case = (saver, *version_case[1:])
        cold_value = {**value, "large_version_probe": list(value["large_version_probe"])}
        if rich_value:
            cold_value["summary_message"] = value["summary_message"].model_copy(deep=True)
        latest = _put(cold_case, child, "version-cold", cold_value)
        assert serde.large_writes == 1
        restored = saver.get_tuple(latest)
        assert restored.checkpoint["channel_values"]["state"] == value
        restored.checkpoint["channel_values"]["state"]["large_version_probe"].append("只修改已读对象")
        assert saver.get_tuple(latest).checkpoint["channel_values"]["state"] == value
    with sqlite3.connect(index) as connection:
        assert connection.execute("SELECT count(*) FROM checkpoint_state_values WHERE channel_name='state'").fetchone() == (1,)
        assert connection.execute("SELECT count(DISTINCT value_id),count(*) FROM checkpoint_channels WHERE channel_name='state'").fetchone() == (1, 3)


@pytest.mark.parametrize("changed", [False, True])
def test_same_version_rejects_in_place_mutation_atomically(version_case, changed):
    saver, _serde, admission, _user, index = version_case
    value = {"large_version_probe": ["原正文"]}
    with _execution_scope(admission):
        root = _put(version_case, build_checkpoint_config(admission.thread_id), "mutation-root", value, changed=True)
        canonical = index.with_name("rollout.jsonl").read_bytes()
        value["large_version_probe"].append("原地篡改")
        with pytest.raises(ValueError, match="同版本异内容"):
            _put(version_case, root, "mutation-rejected", value, changed=changed)
        assert saver.get_tuple(root).checkpoint["channel_values"]["state"] == {"large_version_probe": ["原正文"]}
        assert index.with_name("rollout.jsonl").read_bytes() == canonical
    with sqlite3.connect(index) as connection:
        assert connection.execute("SELECT count(*) FROM checkpoints WHERE checkpoint_id='mutation-rejected'").fetchone() == (0,)


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
async def test_new_version_declaration_must_match_checkpoint_atomically(version_case, asynchronous):
    saver, _serde, admission, user, index = version_case
    value = {"large_version_probe": ["已提交正文"]}
    with _execution_scope(admission):
        root = _put(version_case, build_checkpoint_config(admission.thread_id), "declaration-root", value, changed=True)
        canonical = index.with_name("rollout.jsonl").read_bytes()
        checkpoint = _checkpoint("declaration-rejected", [user], state=value)
        checkpoint["channel_versions"]["state"] = "v1"
        with pytest.raises(ValueError, match="new_versions 与 channel_versions 不一致"):
            if asynchronous:
                await saver.aput(root, checkpoint, {"source": "unit"}, {"state": "v2"})
            else:
                saver.put(root, checkpoint, {"source": "unit"}, {"state": "v2"})
        assert saver.get_tuple(root).checkpoint["channel_values"]["state"] == value
        assert index.with_name("rollout.jsonl").read_bytes() == canonical
        # 相同checkpoint幂等重入也必须校验声明，不能由重试捷径绕过。
        with pytest.raises(ValueError, match="new_versions 与 channel_versions 不一致"):
            saver.put(root, saver.get_tuple(root).checkpoint, {"source": "unit"}, {"state": "v2"})
    with sqlite3.connect(index) as connection:
        assert connection.execute("SELECT count(*) FROM checkpoints WHERE checkpoint_id='declaration-rejected'").fetchone() == (0,)


@pytest.mark.parametrize("collision", [False, True])
def test_schema19_upgrade_deduplicates_or_rejects_conflicting_history(version_case, collision):
    saver, _serde, admission, _user, index = version_case
    value = {"large_version_probe": ["旧库正文"]}
    with _execution_scope(admission):
        root = _put(version_case, build_checkpoint_config(admission.thread_id), "migration-root", value, changed=True)
        child = _put(version_case, root, "migration-child", value)
    with sqlite3.connect(index) as connection:
        restore_schema19(connection)
        if collision:
            serializer, blob = JsonPlusSerializer().dumps_typed({"large_version_probe": ["冲突正文"]})
            connection.execute("UPDATE checkpoint_channels SET serializer_name=?,value_blob=?,value_length=?,value_hash=? "
                               "WHERE checkpoint_id='migration-child' AND channel_name='state'",
                               (serializer, blob, len(blob), hashlib.sha256(blob).hexdigest()))
            connection.commit()
    arguments = {"session_id": SESSION_ID, "to_version": 20, "migration_name": "checkpoint_state_versions", "migration_sql": CHANNEL_VALUES_MIGRATION_SQL}
    if collision:
        with pytest.raises(ValueError, match="同版本异内容"):
            saver._storage.storage.migrate_schema(admission.thread_id, **arguments)
        with sqlite3.connect(index) as connection:
            assert connection.execute("SELECT schema_version,database_state FROM database_meta").fetchone() == (19, "recovery_required")
            assert connection.execute("SELECT count(*) FROM checkpoint_channels WHERE channel_name='state'").fetchone() == (2,)
    else:
        saver._storage.storage.migrate_schema(admission.thread_id, **arguments)
        with _execution_scope(admission):
            assert RolloutCheckpointSaver(saver._storage.storage.sessions_dir).get_tuple(child).checkpoint["channel_values"]["state"] == value
        with sqlite3.connect(index) as connection:
            assert connection.execute("SELECT count(*) FROM checkpoint_state_values WHERE channel_name='state'").fetchone() == (1,)


def test_reference_cannot_switch_to_another_authorized_historical_owner(version_case):
    saver, _serde, admission, _user, index = version_case
    with _execution_scope(admission):
        root = _put(version_case, build_checkpoint_config(admission.thread_id), "owner-root", {"large_version_probe": ["当前正文"]}, changed=True)
        with sqlite3.connect(index) as connection:
            binding = HistoricalNamespaceBinding(
                SESSION_ID, "thr_00000000400070008000000000000999", "boxteam://history/source", "source-rollout", "source-snapshot",
            )
            connection.execute("INSERT INTO history_namespace_bindings VALUES (?,?,?,?,?,?)", (*binding.to_dict().values(), sha256_jcs(binding.to_dict())))
            result = connection.execute(
                "INSERT INTO checkpoint_state_values(session_id,thread_id,checkpoint_ns,channel_name,channel_version,serializer_name,value_blob,value_length,value_hash) "
                "SELECT ?,?,checkpoint_ns,channel_name,channel_version,serializer_name,value_blob,value_length,value_hash "
                "FROM checkpoint_state_values WHERE channel_name='state'", (binding.session_id, binding.thread_id),
            )
            connection.execute("UPDATE checkpoint_channels SET value_id=? WHERE checkpoint_id='owner-root' AND channel_name='state'", (result.lastrowid,))
            connection.commit()
        with pytest.raises(RuntimeError, match="版本引用不匹配"):
            saver.get_tuple(root)


def test_equal_channel_version_remains_isolated_between_namespaces(version_case):
    saver, _serde, admission, _user, index = version_case
    expected = {"left": {"large_version_probe": ["左侧正文"]}, "right": {"large_version_probe": ["右侧正文"]}}
    with _execution_scope(admission):
        heads = {
            namespace: _put(version_case, build_checkpoint_config(admission.thread_id, checkpoint_ns=namespace),
                            f"namespace-{namespace}", value, changed=True)
            for namespace, value in expected.items()
        }
        cold = RolloutCheckpointSaver(saver._storage.storage.sessions_dir)
        for namespace, config in heads.items():
            assert cold.get_tuple(config).checkpoint["channel_values"]["state"] == expected[namespace]
    with sqlite3.connect(index) as connection:
        assert connection.execute("SELECT checkpoint_ns,channel_version FROM checkpoint_state_values WHERE channel_name='state' ORDER BY checkpoint_ns").fetchall() == [("left", "v1"), ("right", "v1")]
