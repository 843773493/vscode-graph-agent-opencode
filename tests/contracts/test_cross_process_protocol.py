import json
from pathlib import Path
from typing import cast

import pytest
from google.protobuf import descriptor_pb2
from google.protobuf.json_format import MessageToDict, ParseDict

from app.gateway.protocol.proxy import proxy_target_to_proto
from app.protocol.codecs.browser import browser_page_to_json, browser_page_to_proto
from app.protocol.codecs.session_sse import session_sse_to_json, session_sse_to_proto
from app.protocol.codecs.terminal import (
    terminal_session_to_json,
    terminal_session_to_proto,
)
from app.protocol.generated.boxteam.browser.v1 import browser_pb2
from app.protocol.generated.boxteam.gateway.v1 import proxy_pb2
from app.protocol.generated.boxteam.terminal.v1 import terminal_pb2
from app.protocol.generated.boxteam.workspace.v2 import (
    message_pb2,
    public_pb2,
    session_stream_pb2,
)
from app.schemas.internal_v2.attachment import AttachmentRef
from app.schemas.internal_v2.resource_navigation import (
    ResourceNavigationRequest,
    ResourceNavigationResultDTO,
)
from app.schemas.internal_v2.session_changes import SessionFileChangeDTO
from app.schemas.internal_v2.session_interaction import SessionExecutionSseDTO
from app.schemas.internal_v2.text_mutation import CreateText, TextMutationRequest
from app.schemas.internal_v2.turn import TurnAttachmentDTO
from app.schemas.internal_v2.workspace_file_operations import (
    WorkspaceDirectoryKeyDTO,
    WorkspaceFileListDTO,
)

BASELINE_PATH = Path(__file__).with_name("protocol_baseline.json")


@pytest.mark.parametrize("has_directory", [True, False], ids=["directory", "legacy"])
def test_attachment_and_turn_directory_reference_survive_proto_wire(has_directory: bool) -> None:
    directory = "boxteam://workspace/019d1b4a-1111-7000-8000-000000000001/resources/attachments/att_0123456789abcdef0123456789abcdef"
    payload = {
        "file_id": directory + "/original/content" if has_directory else
            "boxteam://workspace/019d1b4a-1111-7000-8000-000000000001/resources/session/ses_043a718774c572bd8a25adbeb1bfcd5c/attachments/att_0123456789abcdef0123456789abcdef",
        "directory_uri": directory if has_directory else None,
        "name": "report.pdf", "content_type": "application/pdf",
    }
    for schema, protocol in ((AttachmentRef, message_pb2.AttachmentRef), (TurnAttachmentDTO, public_pb2.TurnAttachmentDTO)):
        value = schema.model_validate(payload)
        encoded = ParseDict(value.model_dump(exclude_none=True), protocol()).SerializeToString()
        received = protocol.FromString(encoded)
        assert received.HasField("directory_uri") == has_directory
        assert schema.model_validate(MessageToDict(received, preserving_proto_field_name=True)) == value


def test_session_file_change_wire_and_openapi_use_uri_and_content_without_snapshot_locations() -> None:
    from app.main import app

    value = SessionFileChangeDTO(file_path="boxteam://workspace/absent-target/resources/files/root/%e4%b8%ad.txt",
        kind="edit", latest_edit_id="edit_0123456789ab", diff_text="-before\n+after\n",
        before_preview="before\n", after_preview="after\n")
    assert value.file_path.endswith("/%E4%B8%AD.txt")
    encoded = ParseDict(value.model_dump(exclude_none=True), public_pb2.SessionFileChangeDTO()).SerializeToString()
    received = public_pb2.SessionFileChangeDTO.FromString(encoded)
    assert SessionFileChangeDTO.model_validate(MessageToDict(received, preserving_proto_field_name=True)) == value
    shape = descriptor_pb2.DescriptorProto()
    received.DESCRIPTOR.CopyToProto(shape)
    assert {"before_file", "after_file", "diff_file"} <= set(shape.reserved_name)
    assert all(any(span.start <= number < span.end for span in shape.reserved_range) for number in (10, 11, 12))
    public = app.openapi()["components"]["schemas"]["SessionFileChangeDTO"]
    assert set(public["properties"]) == set(value.model_dump())


def test_resource_read_request_and_media_result_survive_proto_wire() -> None:
    request = ResourceNavigationRequest(uri="boxteam://gateway/gateway_test/resources/skills/demo/image.png",
        operation="read", read_format="auto", line_offset=2, max_lines=10, max_bytes=2048)
    encoded = ParseDict(request.model_dump(exclude_none=True), public_pb2.ResourceNavigationRequest()).SerializeToString()
    received = public_pb2.ResourceNavigationRequest.FromString(encoded)
    assert ResourceNavigationRequest.model_validate(MessageToDict(received, preserving_proto_field_name=True)) == request
    result = ResourceNavigationResultDTO.model_validate({
        "node": {"uri": request.uri, "resource_shape": "file", "operations": ["describe", "read"], "child_path": None},
        "content": "iVBORw==", "content_encoding": "base64", "media_type": "image", "mime_type": "image/png",
        "content_hash": "sha256:" + "a" * 64,
    })
    encoded = ParseDict(result.model_dump(exclude_none=True), public_pb2.ResourceNavigationResultDTO()).SerializeToString()
    received = public_pb2.ResourceNavigationResultDTO.FromString(encoded)
    assert ResourceNavigationResultDTO.model_validate(MessageToDict(received, preserving_proto_field_name=True)) == result


def _baseline() -> dict[str, object]:
    return cast(dict[str, object], json.loads(BASELINE_PATH.read_text(encoding="utf-8")))


def test_representative_boundaries_round_trip_through_protobuf_wire() -> None:
    baseline = _baseline()

    sse = cast(dict[str, object], cast(dict[str, object], baseline["sse"])["job_updated"])["data"]
    sse_value = SessionExecutionSseDTO.model_validate(cast(dict[str, object], sse))
    sse_wire = session_sse_to_proto(sse_value).SerializeToString()
    decoded_sse = session_stream_pb2.SessionExecutionSse.FromString(sse_wire)
    assert session_sse_to_json(decoded_sse)["event"]["type"] == "job.updated"

    terminal = cast(dict[str, object], cast(dict[str, object], baseline["http_json"])["response"])["json"]
    terminal_value = cast(dict[str, object], cast(dict[str, object], terminal)["data"])
    terminal_wire = terminal_session_to_proto(terminal_value).SerializeToString()
    decoded_terminal = terminal_pb2.TerminalSession.FromString(terminal_wire)
    assert terminal_session_to_json(decoded_terminal) == terminal_value

    browser_value = cast(dict[str, object], cast(dict[str, object], cast(dict[str, object], baseline["browser_websocket"])["state"])["state"])
    browser_wire = browser_page_to_proto(browser_value).SerializeToString()
    decoded_browser = browser_pb2.BrowserPage.FromString(browser_wire)
    assert browser_page_to_json(decoded_browser) == browser_value

    proxy_wire = proxy_target_to_proto(
        workspace_id="workspace_123",
        service="terminal",
        path="/api/terminals/terminal_123",
    ).SerializeToString()
    decoded_proxy = proxy_pb2.ProxyTarget.FromString(proxy_wire)
    assert decoded_proxy.workspace_id == "workspace_123"
    assert decoded_proxy.service == "terminal"
    assert decoded_proxy.path == "/api/terminals/terminal_123"


@pytest.mark.parametrize("kind,resource_id", [("files", "root"), ("attachments", "att_0123456789abcdef0123456789abcdef")])
def test_workspace_file_list_directory_identity_survives_proto_wire(kind: str, resource_id: str) -> None:
    workspace = "019d1b4a-1111-7000-8000-000000000001"
    root = f"boxteam://workspace/{workspace}/resources/{kind}/{resource_id}"
    path = root + "/derived"
    key = WorkspaceDirectoryKeyDTO(workspace_id=workspace, scope="workspace", root=root, path=path)
    value = WorkspaceFileListDTO(root_path=root, path=path, items=[{"name": "output.txt", "path": path + "/output.txt", "kind": "file"}],
        directory_key=key, directory_revision=3)
    encoded = ParseDict(value.model_dump(exclude_none=True), public_pb2.WorkspaceFileListDTO()).SerializeToString()
    decoded = public_pb2.WorkspaceFileListDTO.FromString(encoded)
    assert WorkspaceFileListDTO.model_validate(MessageToDict(decoded, preserving_proto_field_name=True)) == value


def test_attachment_text_mutation_keeps_exact_target_through_proto_wire() -> None:
    uri = "boxteam://gateway_0123456789abcdef0123456789abcdef/workspace/019d1b4a-1111-7000-8000-000000000001/resources/attachments/att_0123456789abcdef0123456789abcdef/derived/output.txt"
    value = TextMutationRequest.from_command(CreateText(target=uri, content="处理结果 Ω\n", expected_version="absent"))
    encoded = ParseDict(value.model_dump(exclude_none=True), public_pb2.TextMutationRequest()).SerializeToString()
    decoded = public_pb2.TextMutationRequest.FromString(encoded)
    assert TextMutationRequest.model_validate(MessageToDict(decoded, preserving_proto_field_name=True)) == value
