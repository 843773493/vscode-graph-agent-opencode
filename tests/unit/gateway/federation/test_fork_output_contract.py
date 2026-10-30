"""目标 Gateway 校验 fork 回执后透明传输，不把协议失败伪报为成功。"""

import json

import pytest
from pydantic import ValidationError

from app.gateway.federation.errors import FEDERATION_MALFORMED_FRAME, FederationError
from tests.contracts.api.test_session_fork_uri_contract import (
    INVALID_ADDRESSES,
)
from tests.contracts.api.test_session_fork_uri_contract import (
    fork_output as fork_output,  # noqa: PLC0414
)
from tests.contracts.api.test_session_fork_uri_contract import (
    fork_output_request as fork_output_request,  # noqa: PLC0414
)
from tests.unit.gateway.test_federation_workspace_port import (
    REQUEST_ID,
)
from tests.unit.gateway.test_federation_workspace_port import (
    http_context as http_context,  # noqa: PLC0414
)


@pytest.mark.parametrize("address", INVALID_ADDRESSES)
@pytest.mark.parametrize("location", [
    ("session", "uri"), ("thread_uri",), ("source_uri",),
    ("resources", 0, "resource_uri"), ("resources", 0, "target_uri"),
])
async def test_target_rejects_invalid_fork_output_and_allows_explicit_retry(
    http_context, fork_output_request, fork_output, location, address, caplog,
):
    node = fork_output
    for key in location[:-1]:
        node = node[key]
    original = node[location[-1]]
    node[location[-1]] = address
    # 显式保留稀疏封套和非规范时间文本，校验不能重新序列化合法历史回执。
    fork_output["session"]["created_at"] = "2026-10-30T00:00:00+00:00"
    data = {"result": fork_output}
    http_context.data = {"request_id": REQUEST_ID, "data": data}
    arguments = {"operation": "fork_target", "target": fork_output_request.fork_target.to_payload(),
                 "body": fork_output_request.model_dump(mode="json"),
                 "origin": fork_output_request.source_gateway_id, "timeout": 5}
    with pytest.raises(FederationError) as caught:
        await http_context.port.execute(**arguments)
    assert caught.value.code == FEDERATION_MALFORMED_FRAME
    assert address not in json.dumps(caught.value.to_wire())
    cause = caught.value.__cause__
    assert isinstance(cause, ValidationError)
    assert [(item["loc"], item["input"]) for item in cause.errors()] == [(("result", *location), address)]
    assert any(record.exc_info and record.exc_info[1] is cause for record in caplog.records)
    outgoing, = http_context.requests
    assert outgoing.url.path == "/api/v1/sessions/fork-transfer"
    assert outgoing.headers["X-Request-ID"] == REQUEST_ID
    assert json.loads(outgoing.content) == arguments["body"]

    node[location[-1]] = original
    assert await http_context.port.execute(**arguments) == data
    assert len(http_context.requests) == 2
    assert http_context.requests[0].content == http_context.requests[1].content


@pytest.mark.parametrize("response", [{}, {"intent": {}}, {"result": None}])
async def test_target_rejects_response_for_wrong_fork_phase(
    http_context, fork_output_request, response, caplog,
):
    http_context.data = {"request_id": REQUEST_ID, "data": response}
    with pytest.raises(FederationError) as caught:
        await http_context.port.execute(
            operation="fork_target", target=fork_output_request.fork_target.to_payload(),
            body=fork_output_request.model_dump(mode="json"), origin=fork_output_request.source_gateway_id, timeout=5,
        )
    assert caught.value.code == FEDERATION_MALFORMED_FRAME
    assert isinstance(caught.value.__cause__, ValueError)
    assert "action=create" in str(caught.value.__cause__)
    assert any(record.exc_info and record.exc_info[1] is caught.value.__cause__ for record in caplog.records)
    assert len(http_context.requests) == 1
