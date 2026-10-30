from typing import cast

import pytest
from google.protobuf import descriptor_pb2, descriptor_pool, message_factory

from app.gateway.protocol.control import gateway_health_to_proto
from app.protocol.codecs.browser import browser_page_to_json, browser_page_to_proto
from app.protocol.codecs.terminal import (
    terminal_session_to_json,
    terminal_session_to_proto,
)
from app.protocol.generated.boxteam.terminal.v1 import terminal_pb2
from app.schemas.gateway.diagnostics import GatewayHealthDTO


@pytest.fixture
def terminal_snapshot() -> dict[str, object]:
    return {
        "terminal_id": "terminal_123",
        "workspace_id": "00000000-0000-4000-8000-000000000001",
        "cwd_relative": ".",
        "transport_owner_revision": 9_007_199_254_740_993,
        "command_start_sequence": 9_007_199_254_740_993,
        "status": "running",
        "cols": 120,
        "rows": 32,
        "buffer": "hello\n",
        "last_command_status": "running",
    }


def test_terminal_snapshot_round_trip_preserves_extended_json_fields(terminal_snapshot: dict[str, object]) -> None:
    value = terminal_snapshot
    encoded = terminal_session_to_proto(value)

    decoded = terminal_session_to_json(encoded)
    assert isinstance(decoded["transport_owner_revision"], int)
    assert isinstance(decoded["command_start_sequence"], int)
    assert decoded == value


@pytest.fixture
def extended_terminal_type(monkeypatch: pytest.MonkeyPatch) -> type[terminal_pb2.TerminalSession]:
    """在测试命名空间扩展标量，验证映射不需要另加字段名。"""
    shape = descriptor_pb2.FileDescriptorProto(
        name="test/terminal_codec_extended.proto", package="boxteam.test.terminal_codec",
        syntax="proto3", dependency=[terminal_pb2.DESCRIPTOR.name],
    )
    session = shape.message_type.add()
    terminal_pb2.TerminalSession.DESCRIPTOR.CopyToProto(session)
    for number, (name, kind) in enumerate([
        ("command_label", descriptor_pb2.FieldDescriptorProto.TYPE_STRING),
        ("signal_code", descriptor_pb2.FieldDescriptorProto.TYPE_INT32),
        ("output_count", descriptor_pb2.FieldDescriptorProto.TYPE_UINT32),
        ("input_sequence", descriptor_pb2.FieldDescriptorProto.TYPE_UINT64),
    ], start=18):
        index = len(session.oneof_decl)
        session.oneof_decl.add(name=f"_{name}")
        session.field.add(name=name, number=number, type=kind,
                          label=descriptor_pb2.FieldDescriptorProto.LABEL_OPTIONAL,
                          proto3_optional=True, oneof_index=index)
    pool = descriptor_pool.Default()
    pool.Add(shape)
    message_type = message_factory.GetMessageClass(pool.FindMessageTypeByName("boxteam.test.terminal_codec.TerminalSession"))
    monkeypatch.setattr(terminal_pb2, "TerminalSession", message_type)
    return cast(type[terminal_pb2.TerminalSession], message_type)


@pytest.mark.parametrize("presence", ["omitted", "null", "zero", "value"])
def test_terminal_added_descriptor_fields_survive_wire_without_another_mapping(
    terminal_snapshot: dict[str, object], extended_terminal_type: type[terminal_pb2.TerminalSession], presence: str,
) -> None:
    fields: dict[str, str | int | None] = {
        "command_label": "用户命令", "signal_code": -9,
        "output_count": 7, "input_sequence": 9_007_199_254_740_993,
    }
    if presence == "null":
        fields = dict.fromkeys(fields)
    elif presence == "zero":
        fields = {name: "" if isinstance(value, str) else 0 for name, value in fields.items()}
    if presence != "omitted":
        terminal_snapshot.update(fields)
    encoded = terminal_session_to_proto(terminal_snapshot)
    restored = extended_terminal_type.FromString(encoded.SerializeToString())
    for name in fields:
        assert restored.HasField(name) is (presence in {"zero", "value"})
        if presence in {"zero", "value"}:
            assert getattr(restored, name) == terminal_snapshot[name]
    decoded = terminal_session_to_json(restored)
    assert decoded == terminal_snapshot
    if presence in {"zero", "value"}:
        assert all(type(decoded[name]) is int for name in fields if name != "command_label")


@pytest.mark.parametrize("name", ["title", "cwd_relative"])
def test_terminal_text_fields_keep_strict_validation(terminal_snapshot: dict[str, object], name: str) -> None:
    terminal_snapshot[name] = 42
    with pytest.raises(ValueError, match=f"Terminal 会话字段类型错误: {name}"):
        terminal_session_to_proto(terminal_snapshot)


@pytest.mark.parametrize("name", ["cols", "rows", "sequence", "transport_owner_revision", "command_start_sequence"])
@pytest.mark.parametrize("invalid", [-1, 1.5, "1"])
def test_terminal_unsigned_fields_keep_strict_validation(
    terminal_snapshot: dict[str, object], name: str, invalid: object,
) -> None:
    terminal_snapshot[name] = invalid
    with pytest.raises(ValueError, match=f"Terminal 会话字段必须是非负整数: {name}"):
        terminal_session_to_proto(terminal_snapshot)


@pytest.mark.parametrize("name", ["exit_code", "last_command_exit_code"])
@pytest.mark.parametrize("invalid", [1.5, "1"])
def test_terminal_exit_fields_keep_strict_validation(
    terminal_snapshot: dict[str, object], name: str, invalid: object,
) -> None:
    terminal_snapshot[name] = invalid
    with pytest.raises(ValueError, match=f"Terminal 会话退出码必须是整数: {name}"):
        terminal_session_to_proto(terminal_snapshot)


def test_terminal_completed_snapshot_is_accepted_during_command_release() -> None:
    value = {
        "terminal_id": "terminal_completed",
        "workspace_id": "00000000-0000-4000-8000-000000000001",
        "transport_owner_revision": 1,
        "status": "completed",
        "last_command_status": "completed",
        "last_command_exit_code": 0,
    }

    encoded = terminal_session_to_proto(value)

    assert encoded.status == terminal_pb2.TERMINAL_STATUS_EXITED
    decoded = terminal_session_to_json(encoded)
    assert isinstance(decoded["transport_owner_revision"], int)
    assert decoded == value


def test_browser_snapshot_round_trip_preserves_extended_json_fields() -> None:
    value = {
        "browser_id": "browser_123",
        "page_id": "page_123",
        "workspace_id": "00000000-0000-4000-8000-000000000001",
        "transport_owner_revision": 9_007_199_254_740_993,
        "reconciliation_status": "verified",
        "status": "running",
        "url": "https://example.com",
        "viewport": {"width": 1280, "height": 800},
        "pages": [{"page_id": "page_123", "active": True}],
    }

    encoded = browser_page_to_proto(value)

    decoded = browser_page_to_json(encoded)
    assert isinstance(decoded["transport_owner_revision"], int)
    assert decoded == value


def test_gateway_adapter_uses_only_gateway_protocol_fields() -> None:
    encoded = gateway_health_to_proto(
        GatewayHealthDTO(
            gateway_id="gateway_123",
            process_id=123,
            active_workspace_id="workspace_123",
            development_restart_available=True,
        ),
    )

    assert encoded.gateway_id == "gateway_123"
    assert encoded.active_workspace_id == "workspace_123"
    assert encoded.process_id == 123

@pytest.mark.parametrize("exit_code", [0, 9, -9, -(2**31), 2**31 - 1, None])
def test_terminal_exit_codes_keep_integer_type_and_presence_through_wire(exit_code: int | None) -> None:
    value = {
        "terminal_id": "term_exit_presence",
        "workspace_id": "00000000-0000-4000-8000-000000000001",
        "transport_owner_revision": 1,
        "status": "running" if exit_code is None else "completed",
        "exit_code": exit_code, "last_command_exit_code": exit_code,
    }
    encoded = terminal_session_to_proto(value)
    wire = terminal_pb2.TerminalSession.FromString(encoded.SerializeToString())
    decoded = terminal_session_to_json(wire)
    for field in ("exit_code", "last_command_exit_code"):
        assert wire.HasField(field) is (exit_code is not None)
        assert decoded[field] == exit_code
        if exit_code is not None:
            assert isinstance(decoded[field], int)
