import errno
import json
from datetime import UTC, datetime
from functools import partial
from pathlib import Path

import pytest
from langchain_core.messages import HumanMessage

from app.domain.itemized.execution_admission import (
    ExecutionBinding,
    InternalExecutionIntent,
    UserExecutionIdentity,
)
from app.prompting import internal_message_factory
from app.schemas.internal_v2.common import MessageRole
from app.schemas.internal_v2.message import MessageDTO
from app.schemas.internal_v2.pending_request import (
    PendingRequestControl,
    PendingRequestDTO,
)
from app.services.business.message_service import MessageService
from app.services.infrastructure import pending_request_store as pending_store_module
from app.services.infrastructure.pending_request_store import PendingRequestStore
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from tests.support.message_service import build_message_service


@pytest.fixture
def pending_input_owner(tmp_path) -> MessageService:
    sessions_dir = tmp_path / "sessions"
    return build_message_service(sessions_dir, checkpointer=RolloutCheckpointSaver(sessions_dir=sessions_dir), workspace_root=tmp_path)


@pytest.fixture
def request_factory(pending_input_owner):
    return partial(_request, input_owner=pending_input_owner)


@pytest.fixture
def store_factory(pending_input_owner):
    def make(sessions_dir):
        return PendingRequestStore(sessions_dir=sessions_dir, input_reader=lambda reference: pending_input_owner.accepted_job_message(reference, display=False))
    return make


def _request(
    session_id: str,
    *,
    input_owner: MessageService,
    sequence: int,
    content: str = "内容",
    gateway_id: str | None = "gateway_test",
    request_id: str | None = "request_test",
    execution_binding: ExecutionBinding | None = None,
) -> PendingRequestDTO:
    now = datetime.now(UTC)
    job_id = f"job_{sequence}"
    message_id = f"msg_{sequence}"
    binding = execution_binding or UserExecutionIdentity(job_id=job_id, turn_id=f"turn_{sequence}", admission_idempotency_key=f"adm_{sequence}")
    request = PendingRequestDTO(
        job_id=job_id,
        execution_binding=binding,
        message_id=message_id,
        session_id=session_id,
        thread_id=input_owner._resolve_main_thread_id(session_id),
        turn_id=binding.turn_id if isinstance(binding, UserExecutionIdentity) else None,
        content=content,
        delivery_policy="after_turn",
        enqueue_sequence=sequence,
        position=sequence - 1,
        agent_id="default",
        message_created_at=now.isoformat(),
        created_at=now,
        updated_at=now,
        snapshot_version=sequence,
        gateway_id=gateway_id,
        request_id=request_id,
    )
    if isinstance(binding, UserExecutionIdentity):
        input_owner.accept_user_message(MessageDTO(
            message_id=message_id, session_id=session_id, thread_id=request.thread_id,
            role=MessageRole.user, content=content, created_at=now, updated_at=now,
        ), execution_identity=binding)
    else:
        prepared = internal_message_factory.build(kind="checkpoint_reminder", control=content)
        input_owner._checkpointer.admit_internal_execution(
            session_id, thread_id=request.thread_id, job_id=job_id, intent=binding,
            message=HumanMessage(id=message_id, content=prepared.content, response_metadata={
                "message_id": message_id, "created_at": now.isoformat(), "message_metadata": prepared.metadata,
            }),
        )
    reference = PendingRequestControl.model_validate(request.model_dump(exclude={"content", "attachments", "message_created_at", "message_metadata"}))
    message = input_owner.accepted_job_message(reference, display=False)
    return request.model_copy(update={"content": message.content, "attachments": message.attachments, "message_created_at": message.created_at.isoformat(), "message_metadata": {**message.metadata, "boxteam_session_provider_id": None}})


@pytest.mark.asyncio
async def test_pending_request_store_round_trip(tmp_path, session_bundle_factory, request_factory, store_factory, pending_input_owner):
    sessions_dir = tmp_path / "sessions"
    session_id = "ses_019c71c26c4f7859827c53d8b2f17d9b"
    session_dir = session_bundle_factory(sessions_dir, session_id)
    store = store_factory(sessions_dir)
    request = request_factory(session_id, sequence=1)

    await store.save(session_id, [request])
    restored = await store.load(session_id)

    assert restored == [request]
    assert restored[0].execution_binding == request.execution_binding
    assert (session_dir / "pending_requests.json").is_file()
    _header, detail = (session_dir / "pending_requests.json").read_text().split("\n", 1)
    assert not {"content", "attachments", "message_created_at", "message_metadata"} & json.loads(detail)[0].keys()


@pytest.mark.asyncio
async def test_cold_thread_summaries_read_control_references_beyond_header_without_input(
    tmp_path, session_bundle_factory, request_factory, pending_input_owner,
) -> None:
    session_id = "ses_019c71c26c4f7859827c53d8b2f17d9b"
    sessions_dir = tmp_path / "sessions"
    session_dir = session_bundle_factory(sessions_dir, session_id)
    main_thread = pending_input_owner._resolve_main_thread_id(session_id)
    child_thread = "thr_018f0000000070008000000000000002"
    # 控制引用 fixture：header 的前八项全属 child，main 在后续 detail 中。
    references = [
        PendingRequestStore._control_reference(request_factory(session_id, sequence=index))
        .model_copy(update={"thread_id": child_thread if index <= 8 else main_thread})
        for index in range(1, 11)
    ]
    (session_dir / "pending_requests.json").write_bytes(PendingRequestStore._serialize_current(references))

    def reject_input_read(_reference):
        raise AssertionError("冷摘要不得读取 canonical 正文或恢复执行")

    store = PendingRequestStore(sessions_dir=sessions_dir, input_reader=reject_input_read)
    aggregate = await store.load_summaries(session_id, limit=8)
    assert aggregate.thread_id is None and aggregate.request_count == 10
    assert [item.thread_id for item in aggregate.requests] == [child_thread] * 8

    main = await store.load_summaries(session_id, thread_id=main_thread, limit=1)
    assert main.thread_id == main_thread and main.request_count == 2 and main.truncated is True
    assert [(item.job_id, item.thread_id) for item in main.requests] == [("job_9", main_thread)]
    empty = await store.load_summaries(session_id, thread_id="thr_absent", limit=8)
    assert empty.requests == [] and empty.request_count == 0 and empty.truncated is False


@pytest.mark.asyncio
async def test_cold_thread_summary_keeps_one_version_during_atomic_replace(
    tmp_path, monkeypatch, session_bundle_factory, request_factory, pending_input_owner,
) -> None:
    session_id = "ses_019c71c26c4f7859827c53d8b2f17d9b"
    sessions_dir = tmp_path / "sessions"
    session_dir = session_bundle_factory(sessions_dir, session_id)
    path = session_dir / "pending_requests.json"
    main_thread = pending_input_owner._resolve_main_thread_id(session_id)
    old = PendingRequestStore._serialize_current([
        PendingRequestStore._control_reference(request_factory(session_id, sequence=1)),
    ])
    new = PendingRequestStore._serialize_current([
        PendingRequestStore._control_reference(request_factory(session_id, sequence=2)),
    ])
    path.write_bytes(old)
    original_open = Path.open
    replaced = False

    class ReplacingReader:
        def __init__(self, stream):
            self.stream = stream

        def __enter__(self):
            self.stream.__enter__()
            return self

        def __exit__(self, *args):
            return self.stream.__exit__(*args)

        def readline(self, size):
            nonlocal replaced
            line = self.stream.readline(size)
            if not replaced:
                replaced = True
                PendingRequestStore._atomic_write(path, new)
            return line

        def read(self):
            return self.stream.read()

    def replacing_open(opened_path, mode="r", *args, **kwargs):
        stream = original_open(opened_path, mode, *args, **kwargs)
        return ReplacingReader(stream) if opened_path == path and mode == "rb" else stream

    monkeypatch.setattr(Path, "open", replacing_open)

    def reject_input_read(_reference):
        raise AssertionError("冷摘要不能读取用户输入")

    store = PendingRequestStore(sessions_dir=sessions_dir, input_reader=reject_input_read)
    first = await store.load_summaries(session_id, thread_id=main_thread, limit=8)
    assert first.snapshot_version == 1 and [item.job_id for item in first.requests] == ["job_1"]
    second = await store.load_summaries(session_id, thread_id=main_thread, limit=8)
    assert second.snapshot_version == 2 and [item.job_id for item in second.requests] == ["job_2"]


@pytest.mark.asyncio
async def test_store_validates_canonical_reference_and_ignores_display_body(
    tmp_path, session_bundle_factory, request_factory, store_factory,
):
    session_id = "ses_019c71c26c4f7859827c53d8b2f17d9b"
    session_dir = session_bundle_factory(tmp_path / "sessions", session_id)
    store = store_factory(tmp_path / "sessions")
    request = request_factory(session_id, sequence=1, content="已接受的不可变正文")
    await store.save(session_id, [request.model_copy(update={"content": "伪造展示镜像"})])
    assert (await store.load(session_id))[0].content == "已接受的不可变正文"
    original = (session_dir / "pending_requests.json").read_bytes()
    with pytest.raises((KeyError, ValueError), match="stage=project_input") as caught:
        await store.save(session_id, [request.model_copy(update={"message_id": "msg_unaccepted"})])
    assert isinstance(caught.value.__cause__, (KeyError, ValueError))
    assert any(term in str(caught.value.__cause__) for term in ("admission", "identity", "一致"))
    assert (session_dir / "pending_requests.json").read_bytes() == original


@pytest.mark.asyncio
async def test_pending_request_store_round_trip_preserves_internal_execution_intent(
    tmp_path,
    session_bundle_factory,
    request_factory, store_factory, pending_input_owner,
):
    sessions_dir = tmp_path / "sessions"
    session_id = "ses_019c71c26c4f7859827c53d8b2f17d9b"
    session_bundle_factory(sessions_dir, session_id)
    store = store_factory(sessions_dir)
    intent = InternalExecutionIntent(
        message_id="msg_1",
        execution_id="intr_round_trip",
        admission_idempotency_key="adm_round_trip",
    )
    request = request_factory(
        session_id,
        sequence=1,
        execution_binding=intent,
    )

    await store.save(session_id, [request])
    restored = await store.load(session_id)

    assert restored[0].execution_binding == intent


@pytest.mark.asyncio
async def test_pending_request_store_rejects_missing_execution_binding(
    tmp_path,
    session_bundle_factory,
    request_factory, store_factory, pending_input_owner,
):
    sessions_dir = tmp_path / "sessions"
    session_id = "ses_019c71c26c4f7859827c53d8b2f17d9b"
    session_dir = session_bundle_factory(sessions_dir, session_id)
    store = store_factory(sessions_dir)
    await store.save(session_id, [request_factory(session_id, sequence=1)])
    path = session_dir / "pending_requests.json"
    header, detail = path.read_text(encoding="utf-8").split("\n", 1)
    records = json.loads(detail)
    records[0].pop("execution_binding")
    path.write_text(
        header + "\n" + json.dumps(records, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="待处理队列恢复失败"):
        await store.load(session_id)


def test_pending_request_rejects_binding_for_different_message(tmp_path, session_bundle_factory, request_factory) -> None:
    session_bundle_factory(tmp_path / "sessions", "ses_019c71c26c4f7859827c53d8b2f17d9b")
    with pytest.raises(ValueError, match="消息 identity 不一致"):
        request_factory(
            "ses_019c71c26c4f7859827c53d8b2f17d9b",
            sequence=1,
            execution_binding=InternalExecutionIntent(
                message_id="msg_other",
                execution_id="intr_other",
                admission_idempotency_key="adm_other",
            ),
        )


@pytest.mark.asyncio
async def test_pending_request_store_round_trip_preserves_gateway_id(
    tmp_path,
    session_bundle_factory,
    request_factory, store_factory, pending_input_owner,
):
    """gateway_id 是 Job 作为独立执行根的身份，落盘再读回必须逐字一致。"""
    sessions_dir = tmp_path / "sessions"
    session_id = "ses_019c71c26c4f7859827c53d8b2f17d9b"
    session_bundle_factory(sessions_dir, session_id)
    store = store_factory(sessions_dir)
    request = request_factory(session_id, sequence=1, gateway_id="gateway_abcd1234")

    await store.save(session_id, [request])
    restored = await store.load(session_id)

    assert [item.gateway_id for item in restored] == ["gateway_abcd1234"]


@pytest.mark.asyncio
async def test_pending_request_store_round_trip_preserves_request_id(
    tmp_path,
    session_bundle_factory,
    request_factory, store_factory, pending_input_owner,
):
    """request_id 是 Job 作为独立执行根的身份，落盘再读回必须逐字一致。"""
    sessions_dir = tmp_path / "sessions"
    session_id = "ses_00000000f00070008000000000c4a1d2"
    session_bundle_factory(sessions_dir, session_id)
    store = store_factory(sessions_dir)
    request = request_factory(session_id, sequence=1, request_id="request_abcd1234")

    await store.save(session_id, [request])
    restored = await store.load(session_id)

    assert [item.request_id for item in restored] == ["request_abcd1234"]


@pytest.mark.asyncio
async def test_pending_request_store_rejects_record_missing_gateway_id(
    tmp_path,
    session_bundle_factory,
    request_factory, store_factory, pending_input_owner,
):
    """老数据缺 gateway_id 时必须诚实失败，绝不静默补字面量或默认值。"""
    sessions_dir = tmp_path / "sessions"
    session_id = "ses_019b893703987b7faae8c21f93eddc34"
    session_dir = session_bundle_factory(sessions_dir, session_id)
    store = store_factory(sessions_dir)
    await store.save(session_id, [request_factory(session_id, sequence=1)])
    path = session_dir / "pending_requests.json"
    header, detail = path.read_text(encoding="utf-8").split("\n", 1)
    records = json.loads(detail)
    records[0].pop("gateway_id")
    path.write_text(
        header + "\n" + json.dumps(records, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="待处理队列恢复失败"):
        await store.load(session_id)


@pytest.mark.asyncio
async def test_pending_request_store_rejects_record_missing_request_id(
    tmp_path,
    session_bundle_factory,
    request_factory, store_factory, pending_input_owner,
):
    """老数据缺 request_id 时必须诚实失败，绝不静默补字面量或默认值。"""
    sessions_dir = tmp_path / "sessions"
    session_id = "ses_00000000f00070008000000000c4a1d2"
    session_dir = session_bundle_factory(sessions_dir, session_id)
    store = store_factory(sessions_dir)
    await store.save(session_id, [request_factory(session_id, sequence=1)])
    path = session_dir / "pending_requests.json"
    header, detail = path.read_text(encoding="utf-8").split("\n", 1)
    records = json.loads(detail)
    records[0].pop("request_id")
    path.write_text(
        header + "\n" + json.dumps(records, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="待处理队列恢复失败"):
        await store.load(session_id)


@pytest.mark.asyncio
async def test_pending_summary_read_is_bounded_and_skips_full_detail(
    tmp_path,
    session_bundle_factory,
    request_factory, store_factory, pending_input_owner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions_dir = tmp_path / "sessions"
    session_id = "ses_019b893703987b7faae8c21f93eddc34"
    session_dir = session_bundle_factory(sessions_dir, session_id)
    store = store_factory(sessions_dir)
    requests = [
        request_factory(
            session_id,
            sequence=index + 1,
            content="x" * (512 * 1024),
        )
        for index in range(40)
    ]
    await store.save(session_id, requests)
    pending_path = session_dir / "pending_requests.json"
    original_open = Path.open
    bytes_read = 0

    class BoundedHeaderReader:
        def __init__(self, stream) -> None:
            self._stream = stream

        def __enter__(self):
            self._stream.__enter__()
            return self

        def __exit__(self, *args):
            return self._stream.__exit__(*args)

        def readline(self, size: int = -1):
            nonlocal bytes_read
            assert 0 < size <= 64 * 1024 + 1
            payload = self._stream.readline(size)
            bytes_read += len(payload)
            return payload

        def read(self, *args, **kwargs):
            raise AssertionError("摘要读取不得加载完整 pending detail")

    def bounded_open(path: Path, mode: str = "r", *args, **kwargs):
        stream = original_open(path, mode, *args, **kwargs)
        if path == pending_path and mode == "rb":
            return BoundedHeaderReader(stream)
        return stream

    monkeypatch.setattr(Path, "open", bounded_open)
    summaries = await store.load_summaries(session_id, limit=8)

    assert [item.job_id for item in summaries.requests] == [
        f"job_{index}" for index in range(1, 9)
    ]
    assert summaries.request_count == 40
    assert summaries.truncated is True
    assert bytes_read <= 64 * 1024


@pytest.mark.asyncio
async def test_legacy_pending_schema_is_rejected_without_compatibility_migration(
    tmp_path,
    session_bundle_factory,
    request_factory, store_factory, pending_input_owner,
) -> None:
    sessions_dir = tmp_path / "sessions"
    session_id = "ses_019c68b3cc6b735b85733e284efa48c3"
    session_dir = session_bundle_factory(sessions_dir, session_id)
    store = store_factory(sessions_dir)
    path = session_dir / "pending_requests.json"
    legacy = json.dumps(
        [{"job_id": "legacy", "kind": "steering"}],
        ensure_ascii=False,
    ) + "\n"
    path.write_text(legacy, encoding="utf-8")

    with pytest.raises(RuntimeError, match="旧 pending kind") as caught:
        await store.migrate_schema(session_id)
    assert str(path) not in str(caught.value)
    assert "artifact=pending_requests, stage=migrate" in str(caught.value)
    assert path.read_text(encoding="utf-8") == legacy


@pytest.mark.asyncio
async def test_store_rejects_duplicate_queue_sequences(
    tmp_path,
    session_bundle_factory,
    request_factory, store_factory, pending_input_owner,
) -> None:
    sessions_dir = tmp_path / "sessions"
    session_id = "ses_019c31e67b4172ddbaf02f165d52ef8f"
    session_bundle_factory(sessions_dir, session_id)
    store = store_factory(sessions_dir)

    with pytest.raises(RuntimeError, match="重复入队序号"):
        await store.save(
            session_id,
            [request_factory(session_id, sequence=1), request_factory(session_id, sequence=1)],
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["stat", "read", "mkdir", "temporary", "fsync", "replace", "delete", "migrate"])
@pytest.mark.parametrize("error_number", [errno.EACCES, errno.EIO])
async def test_pending_io_errors_keep_class_errno_and_internal_cause(
    tmp_path, session_bundle_factory, request_factory, store_factory, monkeypatch,
    operation, error_number,
) -> None:
    session_id = "ses_019c71c26c4f7859827c53d8b2f17d9b"
    sessions_dir = tmp_path / "sessions"
    session_dir = session_bundle_factory(sessions_dir, session_id)
    path = session_dir / "pending_requests.json"
    store = store_factory(sessions_dir)
    request = request_factory(session_id, sequence=1)
    await store.save(session_id, [request])
    original = path.read_bytes()
    source = OSError(error_number, f"native-storage={path}", str(path))

    def fail(*_args, **_kwargs):
        raise source

    if operation in {"stat", "read", "migrate", "mkdir", "delete"}:
        method = {"stat": "stat", "read": "open", "migrate": "open", "mkdir": "mkdir", "delete": "unlink"}[operation]
        original_method = getattr(Path, method)

        def access(target, *args, **kwargs):
            if target == (session_dir if operation == "mkdir" else path):
                raise source
            return original_method(target, *args, **kwargs)

        monkeypatch.setattr(Path, method, access)
    else:
        owner = pending_store_module.tempfile if operation == "temporary" else pending_store_module.os
        monkeypatch.setattr(owner, "mkstemp" if operation == "temporary" else operation, fail)
    stage = {"stat": "resolve", "read": "read_control", "delete": "delete", "migrate": "migrate"}.get(operation, "write_control")
    failure_type = RuntimeError if operation == "read" else type(source)
    with pytest.raises(failure_type) as caught:
        if operation in {"stat", "read"}:
            await store.load(session_id)
        elif operation == "delete":
            await store.delete(session_id)
        elif operation == "migrate":
            await store.migrate_schema(session_id)
        else:
            await store.save(session_id, [request])
    message = str(caught.value)
    assert session_id in message and "artifact=pending_requests" in message
    assert f"stage={stage}" in message and f"errno={error_number}" in message
    assert str(tmp_path) not in message and "native-storage=" not in message
    native_error = caught.value.__cause__ if operation == "read" else caught.value
    assert type(native_error) is type(source)
    assert native_error.errno == error_number
    assert native_error.filename is None and native_error.filename2 is None
    assert native_error.__cause__ is source
    monkeypatch.undo()
    assert path.read_bytes() == original
    assert list(session_dir.glob(".pending_requests.json.*")) == []
    assert await store.load(session_id) == [request]


@pytest.mark.asyncio
@pytest.mark.parametrize("reader", ["load", "summary", "thread_summary"])
@pytest.mark.parametrize("part", ["header", "detail", "json", "utf8", "detail_json", "detail_utf8"])
async def test_pending_decode_errors_do_not_echo_input_or_field_locations(
    tmp_path, session_bundle_factory, request_factory, store_factory, reader, part,
) -> None:
    session_id = "ses_019c71c26c4f7859827c53d8b2f17d9b"
    sessions_dir = tmp_path / "sessions"
    session_dir = session_bundle_factory(sessions_dir, session_id)
    store = store_factory(sessions_dir)
    request = request_factory(session_id, sequence=1)
    await store.save(session_id, [request])
    path = session_dir / "pending_requests.json"
    original = path.read_bytes()
    header, detail = original.split(b"\n", 1)
    sentinel = str(tmp_path / "private-input-marker")
    if part == "header":
        payload = json.loads(header)
        payload["request_count"] = sentinel
        corrupted = json.dumps(payload).encode() + b"\n" + detail
    elif part == "detail":
        payload = json.loads(detail)
        payload[0][sentinel] = sentinel
        corrupted = header + b"\n" + json.dumps(payload).encode() + b"\n"
    elif part == "json":
        corrupted = b'{"request_count":\n' + detail
    elif part == "utf8":
        corrupted = b'{"request_count":"\xff"}\n' + detail
    elif part == "detail_json":
        corrupted = header + b"\n[\n"
    else:
        corrupted = header + b'\n["\xff"]\n'
    path.write_bytes(corrupted)
    if part.startswith("detail") and reader == "summary":
        # 聚合冷摘要只读 header，不能为诊断偷偷读取 detail。
        assert (await store.load_summaries(session_id, limit=8)).request_count == 1
    else:
        with pytest.raises((RuntimeError, ValueError)) as caught:
            if reader == "load":
                await store.load(session_id)
            else:
                await store.load_summaries(session_id, limit=8, thread_id=
                    request.thread_id if reader == "thread_summary" else None)
        message = str(caught.value)
        assert session_id in message and "artifact=pending_requests" in message
        assert str(tmp_path) not in message and "private-input-marker" not in message
        assert "input_value" not in message and "input_type" not in message
        assert caught.value.__cause__ is not None
        if part in {"header", "detail", "json", "utf8"}:
            assert "rules=" in message
        elif part == "detail_json":
            assert "line=" in message and "column=" in message
        else:
            assert "start=" in message and "end=" in message
    assert path.read_bytes() == corrupted
    path.write_bytes(original)
    assert await store.load(session_id) == [request]


@pytest.mark.asyncio
@pytest.mark.parametrize("rule", [
    "header_incomplete_or_oversized", "header_version_unsupported",
    "header_summary_limit_exceeded", "detail_incomplete",
])
async def test_pending_format_errors_identify_rule_and_preserve_file(
    tmp_path, session_bundle_factory, request_factory, store_factory, rule,
) -> None:
    session_id = "ses_019c71c26c4f7859827c53d8b2f17d9b"
    sessions_dir = tmp_path / "sessions"
    session_dir = session_bundle_factory(sessions_dir, session_id)
    store = store_factory(sessions_dir)
    await store.save(session_id, [request_factory(session_id, sequence=1)])
    path = session_dir / "pending_requests.json"
    header_line, detail = path.read_bytes().split(b"\n", 1)
    header = json.loads(header_line)
    if rule == "header_incomplete_or_oversized":
        corrupted = b"incomplete"
    elif rule == "detail_incomplete":
        corrupted = header_line + b"\n" + detail.rstrip(b"\n")
    else:
        if rule == "header_version_unsupported":
            header["version"] = 4
        else:
            header["summaries"] *= 9
        corrupted = json.dumps(header).encode() + b"\n" + detail
    path.write_bytes(corrupted)
    with pytest.raises(RuntimeError, match=f"rule={rule}") as caught:
        await store.load(session_id)
    assert str(tmp_path) not in str(caught.value)
    assert path.read_bytes() == corrupted
