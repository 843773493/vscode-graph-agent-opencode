import json
from pathlib import Path
from typing import cast

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
from app.protocol.generated.boxteam.workspace.v2 import session_stream_pb2
from app.schemas.internal_v2.session_interaction import SessionExecutionSseDTO

BASELINE_PATH = Path(__file__).with_name("protocol_baseline.json")


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

    browser_value = {
        "browser_id": "browser_123",
        "page_id": "page_123",
        "session_id": "session_123",
        "status": "running",
        "url": cast(dict[str, object], cast(dict[str, object], baseline["browser_websocket"])["state"])["state"]["url"],
    }
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
