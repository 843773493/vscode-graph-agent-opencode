"""未变化的消息通道继承时，stream 语义组不能冒充 canonical projection group。"""

from copy import deepcopy

import pytest
from langchain_core.messages import AIMessage
from langgraph.checkpoint.base import empty_checkpoint

from app.core.checkpoint_config import build_checkpoint_config
from app.core.execution_admission_context import execution_admission_scope
from app.core.session_thread_context import session_thread_owner_scope
from app.domain.itemized.mutation_intents import MutationIntentOwner
from app.services.infrastructure.rollout_context.checkpoint.message_codec import (
    LangChainMessageCodec,
)
from app.services.infrastructure.rollout_context.runtime.stream_accumulator import (
    CanonicalBlockAccumulator,
)


@pytest.fixture
def stream_group_checkpoint(source_seal_case):
    case = source_seal_case
    saver, owner = case.saver, case.owner
    execution = str(case.accepted["initial_execution_id"])
    admission = saver.execution_admission(owner.session_id, thread_id=owner.thread_id, execution_id=execution)
    stream = CanonicalBlockAccumulator(
        turn_id=admission.turn_id, execution_id=execution,
        producer_id="parallel-model-call", model_call_id="parallel-model-call",
    )
    for index, call in enumerate(("first", "second")):
        stream.accept({
            "block_id": call, "block_index": index, "carrier_type": "tool_call",
            "tool_call_id": call, "name": "read", "args": {"path": f"{call}.txt"},
        })
    codec = LangChainMessageCodec()
    storage = saver._storage.storage
    with (
        session_thread_owner_scope(MutationIntentOwner(owner.session_id, owner.thread_id), langgraph_thread_id=owner.thread_id),
        execution_admission_scope(admission, langgraph_thread_id=owner.thread_id),
    ):
        root, = storage.read_items(owner.session_id, owner.thread_id)
        items = stream.finalize(first_item_sequence=2)
        saver.append_items(owner.session_id, items, thread_id=owner.thread_id)
        assert items[0].message_group_id == items[1].message_group_id
        assert all(item.projection_group is None for item in items)
        checkpoint = empty_checkpoint()
        checkpoint["id"] = "parallel-parent"
        checkpoint["channel_values"] = {"messages": [
            *(codec.from_dict(codec.project_message((item,))) for item in (root, *items)),
            AIMessage(
                id="explicit-group", content="保留有序正文",
                tool_calls=[{"id": "third", "name": "read", "args": {"path": "third.txt"}}],
                response_metadata={"model_call_id": "group-model-call"},
            ),
        ]}
        checkpoint["channel_versions"] = {"messages": "1"}
        checkpoint["updated_channels"] = ["messages"]
        config = saver.put(build_checkpoint_config(owner.thread_id), checkpoint, {"source": "unit"}, {"messages": "1"})
        restored = saver.get_tuple(config)
        assert restored is not None
        expected = deepcopy(restored.checkpoint["channel_values"]["messages"])
        assert len(expected[-1].response_metadata["canonical_item_manifests"]) == 2
        yield saver, owner, config, expected


def _successor():
    checkpoint = empty_checkpoint()
    checkpoint["id"] = "parallel-successor"
    checkpoint["channel_values"] = {"counter": 1}
    checkpoint["channel_versions"] = {"messages": "1", "counter": "2"}
    checkpoint["updated_channels"] = ["counter"]
    return checkpoint


@pytest.mark.asyncio
@pytest.mark.parametrize("entrypoint", ["sync", "async"])
async def test_unchanged_messages_inherit_stream_items_and_explicit_group(stream_group_checkpoint, entrypoint):
    saver, owner, config, expected = stream_group_checkpoint
    jsonl = saver._storage.storage.jsonl_path(owner.session_id, thread_id=owner.thread_id)
    original_body = jsonl.read_bytes()
    if entrypoint == "async":
        saved = await saver.aput(config, _successor(), {"source": "unit"}, {"counter": "2"})
        restored = await saver.aget_tuple(saved)
    else:
        saved = saver.put(config, _successor(), {"source": "unit"}, {"counter": "2"})
        restored = saver.get_tuple(saved)
    assert restored.checkpoint["channel_values"]["messages"] == expected
    assert [message.tool_calls[0]["id"] for message in expected[1:3]] == ["first", "second"]
    assert len(expected[-1].response_metadata["canonical_item_manifests"]) == 2
    assert jsonl.read_bytes() == original_body
    with type(saver)(saver._storage.storage.sessions_dir) as reopened:
        assert reopened.get_tuple(saved).checkpoint["channel_values"]["messages"] == expected


def test_incomplete_explicit_group_still_rejects_checkpoint(stream_group_checkpoint):
    saver, owner, config, expected = stream_group_checkpoint
    storage = saver._storage.storage
    member = expected[-1].response_metadata["canonical_item_manifests"][0]["item_id"]
    with storage.write_connection(owner.session_id, owner.thread_id) as connection:
        connection.execute("DELETE FROM item_catalog WHERE item_id=?", (member,))
        connection.commit()
    jsonl = storage.jsonl_path(owner.session_id, thread_id=owner.thread_id)
    original_body = jsonl.read_bytes()
    with pytest.raises(RuntimeError, match="继承消息 selection 缺少完整 canonical group"):
        saver.put(config, _successor(), {"source": "unit"}, {"counter": "2"})
    with storage.read_connection(owner.session_id, owner.thread_id) as connection:
        assert connection.execute("SELECT checkpoint_id FROM checkpoints").fetchall() == [("parallel-parent",)]
    assert jsonl.read_bytes() == original_body
