import os
import shutil
import subprocess
from pathlib import Path

import pytest
from google.protobuf import descriptor_pb2

from app.protocol.generated.boxteam.gateway.v1 import public_pb2 as gateway_pb2
from app.protocol.generated.boxteam.workspace.v2 import (
    job_pb2,
    public_pb2,
    session_interaction_pb2,
)


def test_python_binding_preserves_cross_file_import_and_oneof() -> None:
    event = session_interaction_pb2.SessionExecutionEvent(
        type="job.updated",
        header=session_interaction_pb2.SessionExecutionEventHeader(
            event_id="event_123",
            session_id="session_123",
        ),
        job_updated=job_pb2.JobProgress(
            job_id="job_123",
            status=job_pb2.JOB_STATUS_RUNNING,
            progress=42,
        ),
    )

    assert event.WhichOneof("payload") == "job_updated"
    assert event.job_updated.progress == 42


def test_current_job_and_pending_request_owner_fields_keep_distinct_wire_tags() -> None:
    for message_type in (job_pb2.Job, public_pb2.JobDTO):
        field = message_type.DESCRIPTOR.fields_by_name["thread_id"]
        assert field.number == 14
        value = message_type(job_id="job_fixture", thread_id="thr_fixture")
        decoded = message_type.FromString(value.SerializeToString())
        assert decoded.thread_id == "thr_fixture"
    fields = public_pb2.PendingRequestDTO.DESCRIPTOR.fields_by_name
    assert fields["thread_id"].number == 19
    assert fields["turn_id"].number == 20
    assert fields["execution_binding"].number == 21
    pending = public_pb2.PendingRequestDTO(thread_id="thr_fixture")
    assert not pending.HasField("turn_id")
    pending.turn_id = "turn_fixture"
    decoded = public_pb2.PendingRequestDTO.FromString(pending.SerializeToString())
    assert decoded.HasField("turn_id") and decoded.turn_id == "turn_fixture"


def test_message_owner_survives_public_binding_round_trip() -> None:
    for message_type, tag in ((public_pb2.MessageDTO, 9), (session_interaction_pb2.MessageObservation, 8)):
        assert message_type.DESCRIPTOR.fields_by_name["thread_id"].number == tag
        value = message_type(session_id="ses_owner", thread_id="thr_child")
        decoded = message_type.FromString(value.SerializeToString())
        assert decoded.session_id == "ses_owner"
        assert decoded.thread_id == "thr_child"


def test_resource_navigation_wire_keeps_bounded_request_and_lightweight_nodes() -> None:
    request_fields = public_pb2.ResourceNavigationRequest.DESCRIPTOR.fields_by_name
    assert {name: field.number for name, field in request_fields.items()} == {
        "uri": 1, "operation": 2, "query": 3, "limit": 4, "max_bytes": 5,
        "cursor": 6, "timeout_ms": 7, "scan_limit": 8, "max_response_bytes": 9,
        "read_format": 10, "line_offset": 11, "max_lines": 12,
        "search_mode": 13, "file_glob": 14, "max_snippet_bytes": 15,
    }
    request = public_pb2.ResourceNavigationRequest(
        uri="boxteam://workspace/ws-1/resources/files/root", operation="search",
        query="中文 文档", limit=7, max_bytes=1024,
        cursor="opaque-cursor", timeout_ms=1500, scan_limit=200, max_response_bytes=8192,
    )
    decoded_request = type(request).FromString(request.SerializeToString())
    assert decoded_request == request and decoded_request.HasField("query") and decoded_request.HasField("cursor")
    assert not type(request)().HasField("query")
    request.query = ""
    assert type(request).FromString(request.SerializeToString()).HasField("query")

    node_fields = public_pb2.ResourceNavigationNodeDTO.DESCRIPTOR.fields_by_name
    assert {name: field.number for name, field in node_fields.items()} == {
        "uri": 1, "resource_shape": 2, "operations": 3, "child_path": 4,
    }
    root = public_pb2.ResourceNavigationNodeDTO(
        uri=decoded_request.uri, resource_shape="directory",
        operations=["describe", "list", "search"], child_path="file_segments",
    )
    decoded_root = type(root).FromString(root.SerializeToString())
    assert decoded_root == root and decoded_root.HasField("child_path")
    file = public_pb2.ResourceNavigationNodeDTO(
        uri=root.uri + "/SKILL.md", resource_shape="file", operations=["describe", "read"],
    )
    decoded_file = type(file).FromString(file.SerializeToString())
    assert decoded_file == file and not decoded_file.HasField("child_path")

    result_fields = public_pb2.ResourceNavigationResultDTO.DESCRIPTOR.fields_by_name
    assert {name: field.number for name, field in result_fields.items()} == {
        "node": 1, "entries": 2, "content": 3, "truncated": 4,
        "next_cursor": 5, "truncation_reason": 6, "view": 7,
        "content_encoding": 8, "media_type": 9, "mime_type": 10, "line_offset": 11,
        "matches": 12, "content_hash": 13,
    }
    listing = public_pb2.ResourceNavigationResultDTO(node=root, entries=[file], truncated=True,
        next_cursor="opaque-next", truncation_reason="entry_limit", view="live")
    decoded_listing = type(listing).FromString(listing.SerializeToString())
    assert decoded_listing == listing and not decoded_listing.HasField("content")
    read = public_pb2.ResourceNavigationResultDTO(node=file, content="多层正文 Ω\n")
    decoded_read = type(read).FromString(read.SerializeToString())
    assert decoded_read == read and decoded_read.HasField("content")
    read.content = ""
    assert type(read).FromString(read.SerializeToString()).HasField("content")


def test_information_wire_reserves_removed_physical_location_fields() -> None:
    for message_type, field_name, number in (
        (public_pb2.SessionInformationSnapshotDTO, "storage_path", 7),
        (public_pb2.LLMRequestLogRecordDTO, "file_path", 5),
        (public_pb2.RuntimeInfoDTO, "storage", 9),
        (public_pb2.RuntimeStatusDTO, "storage", 9),
        (public_pb2.SessionInformationWorkspaceDTO, "root_path", 3),
        (public_pb2.WorkspaceDTO, "root_path", 2),
        (public_pb2.WorkspaceContextDTO, "root_path", 2),
    ):
        descriptor = message_type.DESCRIPTOR
        assert field_name not in descriptor.fields_by_name
        shape = descriptor_pb2.DescriptorProto()
        descriptor.CopyToProto(shape)
        assert field_name in shape.reserved_name
        assert any(span.start <= number < span.end for span in shape.reserved_range)


def test_communication_wire_uses_one_destination_for_existing_resource_and_fork_targets() -> None:
    descriptor = public_pb2.CommunicationGatewayRequest.DESCRIPTOR
    assert descriptor.fields_by_name["target"].number == 2
    assert descriptor.fields_by_name["resource_target"].number == 4
    assert descriptor.fields_by_name["fork_target"].number == 5
    assert descriptor.fields_by_name["navigation_target"].number == 6
    assert {field.name for field in descriptor.oneofs_by_name["destination"].fields} == {
        "target", "resource_target", "fork_target", "navigation_target",
    }
    value = public_pb2.CommunicationGatewayRequest(operation="read")
    value.target.gateway_id = "gateway-existing"
    value.target.thread_id = "thread-existing"
    assert value.WhichOneof("destination") == "target"
    decoded_target = public_pb2.CommunicationGatewayRequest.FromString(value.SerializeToString())
    assert decoded_target.WhichOneof("destination") == "target"
    assert decoded_target.target.gateway_id == "gateway-existing"
    assert decoded_target.target.thread_id == "thread-existing"

    value.resource_target.gateway_id = "gateway-resource"
    value.resource_target.resource_identity = "resource"
    value.resource_target.uri = "boxteam://resources/resource"
    value.resource_target.scope = "workspace"
    assert value.WhichOneof("destination") == "resource_target"
    assert not value.HasField("target")
    decoded_resource = public_pb2.CommunicationGatewayRequest.FromString(value.SerializeToString())
    assert decoded_resource.WhichOneof("destination") == "resource_target"
    assert decoded_resource.resource_target.gateway_id == "gateway-resource"
    assert decoded_resource.resource_target.resource_identity == "resource"
    assert decoded_resource.resource_target.uri == "boxteam://resources/resource"
    assert decoded_resource.resource_target.scope == "workspace"

    value.fork_target.gateway_id = "gateway-fork"
    value.fork_target.uri = "boxteam://forks/fork_123/source"
    value.fork_target.fork_role = "source"
    assert value.WhichOneof("destination") == "fork_target"
    assert not value.HasField("target")
    assert not value.HasField("resource_target")
    decoded = public_pb2.CommunicationGatewayRequest.FromString(value.SerializeToString())
    assert decoded.WhichOneof("destination") == "fork_target"
    assert decoded.fork_target.gateway_id == "gateway-fork"
    assert decoded.fork_target.uri == "boxteam://forks/fork_123/source"
    assert decoded.fork_target.fork_role == "source"
    assert not decoded.HasField("target")
    assert not decoded.HasField("resource_target")

    value.navigation_target.gateway_id = "gateway-navigation"
    value.navigation_target.uri = "boxteam://workspace/ws-1/resources/files/root"
    navigation = public_pb2.CommunicationGatewayRequest.FromString(value.SerializeToString())
    assert navigation.WhichOneof("destination") == "navigation_target"
    assert not navigation.HasField("fork_target")
    assert navigation.navigation_target.gateway_id == "gateway-navigation"
    assert navigation.navigation_target.uri == value.navigation_target.uri


@pytest.fixture
def failed_protocol_workspace(tmp_path: Path) -> tuple[Path, tuple[Path, ...]]:
    """模拟外部 Buf 生成失败，保留四个现有产物根的实际文件字节。"""
    project = Path.cwd()
    for name in ("buf.yaml", "buf.gen.yaml"):
        shutil.copyfile(project / name, tmp_path / name)
    shutil.copytree(project / "proto", tmp_path / "proto")
    executable = tmp_path / "node_modules" / ".bin" / "buf"
    executable.parent.mkdir(parents=True)
    executable.write_text(
        "#!/usr/bin/env bun\n"
        'import {mkdir,writeFile} from "node:fs/promises";\n'
        'import path from "node:path";\n'
        'const args=process.argv.slice(2);\n'
        'if(args[0]==="generate"){\n'
        'const output=args[args.indexOf("--output")+1];\n'
        'if(output){await mkdir(output,{recursive:true});await writeFile(path.join(output,"partial.ts"),"partial");}\n'
        'process.stderr.write("injected remote generation failure\\n");process.exit(23);\n'
        '}\n',
    )
    executable.chmod(0o755)
    sentinels = tuple(tmp_path / directory / "sentinel" / suffix for directory, suffix in (
        ("app/protocol/generated", "existing.py"),
        ("src/workspace-services/protocol/generated", "existing.js"),
        ("src/clients/web/src/types/protocol_buf_generated", "existing.d.ts"),
        ("src/clients/web/src/types/protocol_generated", "existing.ts"),
    ))
    for path in sentinels:
        path.parent.mkdir(parents=True)
        path.write_bytes(b"original generated bytes\n")
    return tmp_path, sentinels


def test_failed_protocol_generation_preserves_existing_bindings(
    failed_protocol_workspace: tuple[Path, tuple[Path, ...]],
) -> None:
    workspace, sentinels = failed_protocol_workspace
    stage = workspace / "artifacts" / "protocol-stage"
    result = subprocess.run(
        ["bun", str(Path.cwd() / "scripts/generate_protocol.mjs"), "--staging-root", str(stage)],
        cwd=workspace, env={**os.environ, "BOXTEAM_PROJECT_ROOT": str(workspace)},
        capture_output=True, text=True, timeout=15, check=False,
    )
    assert result.returncode != 0 and "injected remote generation failure" in result.stderr
    assert all(path.read_bytes() == b"original generated bytes\n" for path in sentinels)
    assert stage.is_dir() and tuple(stage.iterdir()) == ()


def test_gateway_complete_backend_group_request_and_results_preserve_wire_contract() -> None:
    request = gateway_pb2.GatewayRuntimeGroupRequest(
        expected_backend_generation="generation-current",
        expected_workspace_ids=["gateway-a", "gateway-b"],
        desired_workspace_ids=gateway_pb2.GatewayRuntimeDesiredWorkspaceSet(workspace_ids=["gateway-a", "gateway-c"]),
    )
    fields = request.DESCRIPTOR.fields_by_name
    assert {name: field.number for name, field in fields.items()} == {
        "expected_backend_generation": 1, "expected_workspace_ids": 2, "desired_workspace_ids": 4,
    }
    decoded = type(request).FromString(request.SerializeToString())
    assert decoded.HasField("expected_backend_generation")
    assert decoded.expected_backend_generation == "generation-current"
    assert list(decoded.expected_workspace_ids) == ["gateway-a", "gateway-b"]
    assert decoded.HasField("desired_workspace_ids")
    assert list(decoded.desired_workspace_ids.workspace_ids) == ["gateway-a", "gateway-c"]
    omitted = gateway_pb2.GatewayRuntimeGroupRequest()
    assert not omitted.HasField("desired_workspace_ids")
    explicit_empty = gateway_pb2.GatewayRuntimeGroupRequest(
        expected_backend_generation="generation-current", expected_workspace_ids=["gateway-a"],
        desired_workspace_ids=gateway_pb2.GatewayRuntimeDesiredWorkspaceSet(),
    )
    empty_roundtrip = type(explicit_empty).FromString(explicit_empty.SerializeToString())
    assert empty_roundtrip.HasField("desired_workspace_ids")
    assert list(empty_roundtrip.desired_workspace_ids.workspace_ids) == []
    assert not gateway_pb2.GatewayRuntimeGroupRequest().HasField("expected_backend_generation")
    blocker = gateway_pb2.GatewayRuntimeBlockerDTO(
        workspace_id="gateway-b", kind="proxy_stream", resource_id="route-b", status="active",
    )
    assert blocker.DESCRIPTOR.fields_by_name["workspace_id"].number == 6
    assert type(blocker).FromString(blocker.SerializeToString()).workspace_id == "gateway-b"
    for message_type, generation_tag, affected_tag in (
        (gateway_pb2.GatewayRuntimeRestartResultDTO, 6, 7),
        (gateway_pb2.GatewayRuntimeStateResultDTO, 5, 6),
        (gateway_pb2.GatewayWorkspaceDTO, 19, 20),
        (gateway_pb2.FederationWorkspaceDTO, 8, 9),
    ):
        fields = message_type.DESCRIPTOR.fields_by_name
        assert fields["backend_generation"].number == generation_tag
        assert fields["affected_workspace_ids"].number == affected_tag
        value = message_type(
            backend_generation="generation-current", affected_workspace_ids=["gateway-a", "gateway-b"],
        )
        decoded = message_type.FromString(value.SerializeToString())
        assert decoded.HasField("backend_generation")
        assert decoded.backend_generation == "generation-current"
        assert list(decoded.affected_workspace_ids) == ["gateway-a", "gateway-b"]
        assert not message_type().HasField("backend_generation")
    restarted = gateway_pb2.GatewayRuntimeRestartResultDTO(
        status="blocked", backend_generation="generation-current",
        affected_workspace_ids=["gateway-a", "gateway-b"], blockers=[blocker],
    )
    decoded_result = type(restarted).FromString(restarted.SerializeToString())
    assert decoded_result.status == "blocked"
    assert decoded_result.blockers[0].workspace_id == "gateway-b"
