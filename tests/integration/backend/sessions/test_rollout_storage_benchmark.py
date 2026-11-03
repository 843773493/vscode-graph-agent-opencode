from __future__ import annotations

import json
import statistics
import time
from pathlib import Path

import pytest
from langchain_core.messages import HumanMessage, message_to_dict
from langgraph.checkpoint.base import empty_checkpoint

from app.core.checkpoint_config import build_checkpoint_config
from app.core.execution_admission_context import execution_admission_scope
from app.core.path_utils import get_session_path_resolver
from app.core.session_thread_context import session_thread_owner_scope
from app.domain.itemized.records import user_input_payload
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from app.services.infrastructure.rollout_context.storage import (
    rollout_schema_introspection,
)
from app.services.infrastructure.rollout_context.storage.catalog import integrity
from tests.harness.python.run_context import TestRunContext

SESSION_ID = "ses_019c205391bb7a4b81b6728df70f63e6"


def _checkpoint(checkpoint_id: str, messages: list[HumanMessage]) -> dict[str, object]:
    checkpoint = empty_checkpoint()
    checkpoint["id"] = checkpoint_id
    checkpoint["channel_values"] = {"messages": messages, "counter": len(messages)}
    checkpoint["channel_versions"] = {
        "messages": str(len(messages)),
        "counter": str(len(messages)),
    }
    checkpoint["updated_channels"] = ["messages", "counter"]
    return checkpoint


def test_long_session_persists_message_deltas_without_snapshot_growth(
    tmp_path: Path,
    session_bundle_factory,
) -> None:
    sessions_dir = tmp_path / "sessions"
    session_bundle_factory(sessions_dir, SESSION_ID)
    saver = RolloutCheckpointSaver(sessions_dir)
    owner = saver.forks.source.resolve_main_thread_owner(SESSION_ID)
    thread_id = owner.thread_id
    messages: list[HumanMessage] = []
    naive_snapshot_bytes = 0
    snapshot_payload_bytes = 0
    config = build_checkpoint_config(thread_id)

    for index in range(128):
        message = HumanMessage(
            content=f"历史消息 {index:03d}: " + ("x" * 256),
            id=f"message-{index:03d}",
        )
        messages.append(message)
        snapshot_payload_bytes += len(
            json.dumps(
                message_to_dict(message),
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        )
        # JSON 数组的每份全量快照等于元素字节数、逗号和两侧方括号。
        naive_snapshot_bytes += snapshot_payload_bytes + index + 2
        turn_id = f"storage-benchmark-turn-{index:03d}"
        execution_id = f"storage-benchmark-execution-{index:03d}"
        saver.accept_turn(
            SESSION_ID,
            thread_id=thread_id,
            accepted_ingress_id=message.id,
            acceptance_idempotency_key=f"storage-benchmark-acceptance-{index:03d}",
            job_id=f"storage-benchmark-job-{index:03d}",
            admission_idempotency_key=f"storage-benchmark-admission-{index:03d}",
            turn_id=turn_id,
            root_item_id=f"item-{message.id}",
            payload=user_input_payload(message.content, None),
            initial_execution_id=execution_id,
        )
        admission = saver.execution_admission(
            SESSION_ID,
            thread_id=thread_id,
            execution_id=execution_id,
        )
        with (
            session_thread_owner_scope(owner, langgraph_thread_id=thread_id),
            execution_admission_scope(admission, langgraph_thread_id=thread_id),
        ):
            config = saver.put(
                config,
                _checkpoint(f"checkpoint-{index:03d}", messages),
                {"source": "benchmark", "step": index},
                {"messages": str(index + 1), "counter": str(index + 1)},
            )

    root = (
        get_session_path_resolver(sessions_dir).resolve_thread_node(
            SESSION_ID, saver._storage.storage.main_thread_id(SESSION_ID)
        )
        / "rollout"
    )
    rollout_path = root / "rollout.jsonl"
    records = [
        json.loads(line)
        for line in rollout_path.read_text(encoding="utf-8").splitlines()
    ]
    message_records = [
        record for record in records if record["semantic_kind"] == "user_input"
    ]
    rollout_bytes = rollout_path.stat().st_size

    assert all(record["format_version"] == 2 for record in records)
    assert all(record["record_type"] == "item" for record in records)
    assert all(
        "role" not in record and "message" not in record and "sequence" not in record
        for record in records
    )
    assert len(message_records) == 128
    assert len({record["item_id"] for record in message_records}) == 128
    assert all(record["wire_role"] == "user" for record in message_records)
    assert rollout_bytes * 8 < naive_snapshot_bytes
    assert not list(root.glob("segment-*.jsonl"))


def test_warm_initialization_measures_body_validation_receipts(
    tmp_path: Path, session_bundle_factory, monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
) -> None:
    sessions = tmp_path / "sessions"
    session_bundle_factory(sessions, SESSION_ID)
    saver = RolloutCheckpointSaver(sessions)
    storage = saver._storage.storage
    thread_id = storage.main_thread_id(SESSION_ID)
    # 关键历史由 Saver 接受入口生成；测量只比较同一份历史的初始化，不计造数时间。
    payload = [{"index": index, "text": "历史输入" * 64} for index in range(128)]
    for index in range(32):
        saver.accept_turn(
            SESSION_ID, thread_id=thread_id, accepted_ingress_id=f"measure-{index}",
            acceptance_idempotency_key=f"measure-{index}", job_id=f"measure-{index}",
            admission_idempotency_key=f"measure-{index}", payload=payload,
            payload_kind="structured_content",
        )
    storage.initialize(SESSION_ID, thread_id=thread_id)
    body_path = storage.jsonl_path(SESSION_ID, thread_id=thread_id)
    body_before = body_path.read_bytes()
    item_count = len(body_before.splitlines())
    validate = rollout_schema_introspection.validate_catalog_body
    canonical_line = integrity.canonical_json_line
    body_checks = 0

    def observe(envelope):
        nonlocal body_checks
        body_checks += 1
        return canonical_line(envelope)

    def without_receipt(raw, **parameters):
        # 仅在测量中取消回执复用；仍经过同一套完整校验，产品不增加关闭检查的配置。
        parameters.pop("cache", None)
        return validate(raw, **parameters)

    measurements = {}
    for mode in ("full_validation", "retained_receipts"):
        body_checks = 0
        durations = []
        with monkeypatch.context() as patch:
            patch.setattr(integrity, "canonical_json_line", observe)
            if mode == "full_validation":
                patch.setattr(rollout_schema_introspection, "validate_catalog_body", without_receipt)
            for _ in range(5):
                start = time.perf_counter_ns()
                storage.initialize(SESSION_ID, thread_id=thread_id)
                durations.append((time.perf_counter_ns() - start) / 1_000_000)
        measurements[mode] = {"elapsed_ms": durations, "median_ms": statistics.median(durations),
                              "body_checks": body_checks}
        assert body_path.read_bytes() == body_before
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    output = context.artifacts_dir / f"body-validation-initialize-{time.time_ns()}.json"
    output.write_text(json.dumps({"items": item_count, "jsonl_bytes": len(body_before),
                                  "measurements": measurements}, indent=2), encoding="utf-8")
    assert measurements["full_validation"]["body_checks"] == item_count * 5
    assert measurements["retained_receipts"]["body_checks"] == 0
