import json
from pathlib import Path
from typing import cast

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
from app.protocol.generated.boxteam.workspace.v2 import public_pb2, session_stream_pb2
from app.schemas.internal_v2.resource_navigation import (
    ResourceNavigationRequest,
    ResourceNavigationResultDTO,
)
from app.schemas.internal_v2.session_interaction import SessionExecutionSseDTO

BASELINE_PATH = Path(__file__).with_name("protocol_baseline.json")


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
