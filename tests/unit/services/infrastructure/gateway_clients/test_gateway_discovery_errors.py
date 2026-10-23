"""通信发现回执损坏时保留错误合同，不把原生位置复制到普通错误。"""

from __future__ import annotations

import copy
from dataclasses import asdict

import httpx
import pytest

from app.services.business.communication.addresses import GlobalThreadAddress
from app.services.business.communication.errors import CommunicationContractError
from app.services.infrastructure.gateway_communication_client import (
    GatewayCommunicationClient,
)
from tests.support.communication import SOURCE_SESSION, SOURCE_THREAD, WORKSPACE_ID
from tests.unit.services.infrastructure.gateway_clients.test_gateway_communication_client import (
    GATEWAY_ID,
    REQUEST_ID,
)
from tests.unit.services.infrastructure.gateway_clients.test_gateway_communication_client import (
    ready_responses as ready_responses,  # noqa: PLC0414 — 复用请求身份和原生授权 fixture
)


@pytest.mark.parametrize("native_path", [
    "/private/discovery/workspace", r"C:\private\discovery\workspace",
    r"\\server\private\discovery\workspace",
])
@pytest.mark.parametrize("damage", ["extra_field", "session_id", "thread_id", "main_pointer"])
async def test_discovery_response_error_keeps_contract_cause_and_same_request_retry(
    ready_responses, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
    native_path: str, damage: str,
) -> None:
    source = GlobalThreadAddress(GATEWAY_ID, WORKSPACE_ID, SOURCE_SESSION, SOURCE_THREAD)
    valid = {"target": asdict(source), "main_thread_id": SOURCE_THREAD}
    damaged = copy.deepcopy(valid)
    if damage == "extra_field":
        damaged["target"][native_path] = "unexpected"
        expected_type = TypeError
    elif damage == "main_pointer":
        damaged["main_thread_id"] = native_path
        expected_type = CommunicationContractError
    else:
        damaged["target"][damage] = native_path
        expected_type = CommunicationContractError
    requests: list[dict[str, object]] = []
    rejected = True

    async def respond(client, method, path, **kwargs):
        requests.append(copy.deepcopy({
            "method": method, "url": str(client.base_url.join(path)),
            "headers": dict(client.headers), "json": kwargs["json"],
            "timeout": client.timeout.as_dict(),
        }))
        return httpx.Response(200, request=httpx.Request(method, client.base_url.join(path)), json={
            "request_id": REQUEST_ID, "data": damaged if rejected else valid,
        }, headers={"X-Request-ID": REQUEST_ID})

    monkeypatch.setattr(httpx.AsyncClient, "request", respond)
    client = GatewayCommunicationClient(gateway_url="http://discovery-errors.test")
    with pytest.raises(expected_type) as caught:
        await client.resolve_main(source=source, session_id=SOURCE_SESSION)
    public = str(caught.value)
    assert native_path not in public and "private" not in public
    assert f"request_id={REQUEST_ID}" in public
    assert "stage=discovery_response_validation" in public
    assert "/api/gateway/federation/discovery" in public
    original = caught.value.__cause__
    assert type(original) is expected_type
    assert (native_path if damage == "extra_field" else repr(native_path)) in str(original)
    if isinstance(caught.value, CommunicationContractError):
        assert caught.value.code == original.code == "communication-target-not-main"
    assert any(record.exc_info and record.exc_info[1] is original for record in caplog.records)
    rejected = False
    binding = await client.resolve_main(source=source, session_id=SOURCE_SESSION)
    assert binding.target == source and binding.target_main_thread_id == SOURCE_THREAD
    assert requests[0] == requests[1]
