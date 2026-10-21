"""pending 消息暂存、canonical 原子发布、冷读和重试合同。"""

from __future__ import annotations

import json
import sqlite3
from copy import deepcopy

import pytest
from langchain_core.messages import AIMessage, HumanMessage, RemoveMessage
from langgraph.graph.message import REMOVE_ALL_MESSAGES
from langgraph.types import Overwrite

from app.core.checkpoint_config import build_checkpoint_config
from app.services.infrastructure.rollout_context.checkpoint.codec.pending_messages import (
    MESSAGE_REFERENCE_SERIALIZER,
)
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from tests.integration.backend.sessions.rollout_context.test_rollout_checkpoint_saver import (
    SESSION_ID,
    _accept_user_execution,
    _checkpoint,
    _execution_scope,
)


@pytest.fixture
def pending_case(tmp_path, session_bundle_factory):
    sessions = tmp_path / "sessions"
    session_bundle_factory(sessions, SESSION_ID)
    saver = RolloutCheckpointSaver(sessions)
    thread = saver.forks.source.resolve_main_thread_owner(SESSION_ID).thread_id
    user = HumanMessage(content="验证 pending 引用", id="pending-user")
    admission = _accept_user_execution(saver, SESSION_ID, thread, user, turn_id="pending-turn")
    index = saver._storage.storage.root(SESSION_ID, thread_id=thread) / "index.sqlite"
    return saver, admission, user, index


def _pending_row(index):
    with sqlite3.connect(index) as connection:
        return connection.execute(
            "SELECT serializer_name,value_blob,value_length,value_hash FROM pending_writes WHERE channel='messages'"
        ).fetchone()



def _descriptor(saver, row):
    return saver._storage.storage.decode_value((row[0][len(MESSAGE_REFERENCE_SERIALIZER):], row[1]))


def _replace_descriptor(saver, index, value):
    serializer, blob, length, digest = saver._storage.storage._encode(value)
    with sqlite3.connect(index) as connection:
        connection.execute(
            "UPDATE pending_writes SET serializer_name=?,value_blob=?,value_length=?,value_hash=?",
            (MESSAGE_REFERENCE_SERIALIZER + serializer, blob, length, digest),
        )


def _cold(saver):
    return RolloutCheckpointSaver(saver._storage.storage.sessions_dir)


@pytest.mark.parametrize("asynchronous", [False, True])
async def test_unpublished_write_survives_restart_then_publishes_lossless_reference(pending_case, asynchronous):
    saver, admission, user, index = pending_case
    answer = AIMessage(content="不可变正文 Ω" * 3000, id="pending-answer", response_metadata={"finish_reason": "stop"})
    with _execution_scope(admission):
        root = saver.put(build_checkpoint_config(admission.thread_id), _checkpoint("pending-root", [user]), {}, {})
        saver.put_writes(root, [("messages", [answer])], "task", "model")
        staged = _pending_row(index)
        assert staged[0] != MESSAGE_REFERENCE_SERIALIZER
        cold = _cold(saver)
        restored = await cold.aget_tuple(root) if asynchronous else cold.get_tuple(root)
        assert restored.pending_writes == [("task", "messages", [answer])]
        checkpoint = _checkpoint("pending-child", [user, answer])
        child = await cold.aput(root, checkpoint, {}, {}) if asynchronous else cold.put(root, checkpoint, {}, {})
        published = _pending_row(index)
        assert published[0].startswith(MESSAGE_REFERENCE_SERIALIZER)
        assert published[2] < staged[2] / 10
        assert answer.content.encode() not in published[1]
        assert answer.content.encode() not in json.dumps(_descriptor(saver, published), ensure_ascii=False).encode()
        reopened = _cold(saver)
        restored = await reopened.aget_tuple(root) if asynchronous else reopened.get_tuple(root)
        assert restored.pending_writes == [("task", "messages", [answer])]
        # 不把 canonical 投影新增的 provenance/metadata 注入原 batch。
        assert restored.pending_writes[0][2][0].model_dump() == answer.model_dump()
        reopened.put_writes(root, [("messages", [answer.model_copy(deep=True)])], "task", "model")
        with pytest.raises(RuntimeError, match="重试内容不一致"):
            reopened.put_writes(root, [("messages", [answer.model_copy(update={"content": "不同正文"})])], "task", "model")
        assert _pending_row(index) == published
        history = await reopened.aget_delta_channel_history(config=child, channels=["messages"]) if asynchronous else reopened.get_delta_channel_history(config=child, channels=["messages"])
        assert history["messages"]["writes"] == [("task", "messages", [answer])]


@pytest.mark.parametrize("shape", ["remove", "clear", "overwrite", "reset", "empty", "tuple"])
def test_message_control_and_batch_boundaries_are_preserved(pending_case, shape):
    saver, admission, user, index = pending_case
    values = {
        "remove": [RemoveMessage(id=user.id)],
        "clear": [RemoveMessage(id=REMOVE_ALL_MESSAGES), user],
        "overwrite": Overwrite([user]), "reset": Overwrite(None),
        "empty": [], "tuple": (user,),
    }
    value = values[shape]
    with _execution_scope(admission):
        root = saver.put(build_checkpoint_config(admission.thread_id), _checkpoint("control-root", [user]), {}, {})
        saver.put_writes(root, [("messages", value)], "task", "control")
        staged = _cold(saver).get_tuple(root).pending_writes
        saver.put(root, _checkpoint("control-child", [user]), {}, {})
        assert _pending_row(index)[0].startswith(MESSAGE_REFERENCE_SERIALIZER)
        # 官方 serializer 将普通 tuple 归一成 list；引用遵循原序列化合同。
        assert _cold(saver).get_tuple(root).pending_writes == staged


def test_failed_checkpoint_leaves_staging_and_jsonl_unchanged(pending_case, monkeypatch):
    saver, admission, user, index = pending_case
    answer = AIMessage(content="待发布正文" * 3000, id="atomic-answer")
    with _execution_scope(admission):
        root = saver.put(build_checkpoint_config(admission.thread_id), _checkpoint("atomic-root", [user]), {}, {})
        saver.put_writes(root, [("messages", [answer])], "task", "model")
        staged = _pending_row(index)
        jsonl = index.with_name("rollout.jsonl")
        before = jsonl.read_bytes()

        def fail(*_args, **_kwargs):
            assert _pending_row(index) == staged  # 另一读连接看不到未提交引用。
            raise RuntimeError("引用发布后的注入失败")

        monkeypatch.setattr(saver._storage.storage, "_insert_checkpoint_channels", fail)
        with pytest.raises(RuntimeError, match="注入失败"):
            saver.put(root, _checkpoint("atomic-child", [user, answer]), {}, {})
        assert jsonl.read_bytes() == before
        assert _pending_row(index) == staged
        assert _cold(saver).get_tuple(root).pending_writes == [("task", "messages", [answer])]


@pytest.mark.parametrize("field", ["session_id", "thread_id", "checkpoint_ns", "group", "item_id"])
def test_reference_requires_exact_historical_owner_namespace_and_version(pending_case, field):
    saver, admission, user, index = pending_case
    with _execution_scope(admission):
        root = saver.put(build_checkpoint_config(admission.thread_id), _checkpoint("owner-root", [user]), {}, {})
        saver.put_writes(root, [("messages", [user])], "task", "model")
        saver.put(root, _checkpoint("owner-child", [user]), {}, {})
        row = _pending_row(index)
        value = _descriptor(saver, row)
        reference = value["write"]["values"][0]["reference"]
        reference[field] = [["item-forged", "sha256:forged"]] if field == "group" else "forged"
        _replace_descriptor(saver, index, value)
        with pytest.raises((RuntimeError, ValueError), match="pending message canonical"):
            _cold(saver).get_tuple(root)


def test_pending_reference_patch_cannot_replace_body_or_identity(pending_case):
    saver, admission, user, index = pending_case
    with _execution_scope(admission):
        root = saver.put(build_checkpoint_config(admission.thread_id), _checkpoint("patch-root", [user]), {}, {})
        saver.put_writes(root, [("messages", [user])], "task", "model")
        saver.put(root, _checkpoint("patch-child", [user]), {}, {})
        original = _descriptor(saver, _pending_row(index))
        for field in ("content", "id"):
            value = deepcopy(original)
            value["write"]["values"][0]["patch"]["changes"].setdefault("data", {"merge": {"changes": {}, "remove": []}})["merge"]["changes"][field] = {"value": "伪造正文或身份"}
            _replace_descriptor(saver, index, value)
            with pytest.raises(ValueError, match="不得改写 canonical 正文"):
                _cold(saver).get_tuple(root)


def test_reference_preserves_official_typed_metadata_serialization(pending_case):
    saver, admission, user, index = pending_case
    typed_user = user.model_copy(update={"additional_kwargs": {"transport_probe": b"\x00\xff"}})
    with _execution_scope(admission):
        root = saver.put(build_checkpoint_config(admission.thread_id), _checkpoint("typed-root", [user]), {}, {})
        saver.put_writes(root, [("messages", [typed_user])], "task", "model")
        staged = _cold(saver).get_tuple(root).pending_writes
        saver.put(root, _checkpoint("typed-child", [user]), {}, {})
        assert _pending_row(index)[0].startswith(MESSAGE_REFERENCE_SERIALIZER)
        assert _cold(saver).get_tuple(root).pending_writes == staged


@pytest.mark.parametrize("corrupt", ["envelope", "fingerprint"])
def test_reference_envelope_and_original_fingerprint_are_validated(pending_case, corrupt):
    saver, admission, user, index = pending_case
    with _execution_scope(admission):
        root = saver.put(build_checkpoint_config(admission.thread_id), _checkpoint("envelope-root", [user]), {}, {})
        saver.put_writes(root, [("messages", [user])], "task", "model")
        saver.put(root, _checkpoint("envelope-child", [user]), {}, {})
        value = _descriptor(saver, _pending_row(index))
        if corrupt == "envelope":
            value["unregistered"] = True
        else:
            value["original"]["hash"] = "invalid"
        _replace_descriptor(saver, index, value)
        with pytest.raises(ValueError, match="envelope 非法|fingerprint 非法"):
            _cold(saver).get_tuple(root)
