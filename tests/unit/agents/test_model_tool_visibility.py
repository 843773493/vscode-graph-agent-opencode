from unittest.mock import MagicMock

from langchain.agents.middleware import ModelRequest

from app.agents.model_tool_visibility import ModelToolVisibilityMiddleware


def test_model_tool_visibility_filters_only_model_request_tools() -> None:
    visible = {"name": "read_file", "description": "读取文件"}
    hidden = {"name": "write_file", "description": "写入文件"}
    request = ModelRequest(
        model=MagicMock(),
        messages=[],
        tools=[visible, hidden],
    )

    filtered = ModelToolVisibilityMiddleware({"write_file"})._visible_request(request)

    assert request.tools == [visible, hidden]
    assert filtered.tools == [visible]


def test_model_tool_visibility_keeps_request_identity_when_nothing_is_hidden() -> None:
    request = ModelRequest(model=MagicMock(), messages=[], tools=[])

    filtered = ModelToolVisibilityMiddleware(set())._visible_request(request)

    assert filtered is request


def test_model_tool_visibility_wraps_model_handler_without_touching_agent_tools() -> None:
    hidden = {"name": "write_file", "description": "写入文件"}
    request = ModelRequest(
        model=MagicMock(),
        messages=[],
        tools=[{"name": "read_file"}, hidden],
    )
    observed: list[ModelRequest] = []

    def handler(next_request: ModelRequest):
        observed.append(next_request)
        return "response"

    response = ModelToolVisibilityMiddleware({"write_file"}).wrap_model_call(
        request,
        handler,
    )

    assert response == "response"
    assert observed[0].tools == [{"name": "read_file"}]
    assert request.tools == [{"name": "read_file"}, hidden]


async def test_model_tool_visibility_wraps_async_model_handler() -> None:
    request = ModelRequest(
        model=MagicMock(),
        messages=[],
        tools=[{"name": "read_file"}, {"name": "write_file"}],
    )
    observed: list[ModelRequest] = []

    async def handler(next_request: ModelRequest):
        observed.append(next_request)
        return "async-response"

    response = await ModelToolVisibilityMiddleware({"write_file"}).awrap_model_call(
        request,
        handler,
    )

    assert response == "async-response"
    assert observed[0].tools == [{"name": "read_file"}]
