from __future__ import annotations

from collections.abc import Mapping
from unittest.mock import AsyncMock

import pytest

from app.services.orchestration.activity_runtime import (
    ActivityCapabilities,
    ActivityHandlerRegistry,
    ActivityRuntime,
)


@pytest.fixture()
def activity_writer() -> AsyncMock:
    message_stream_writer = AsyncMock()
    message_stream_writer.commit.side_effect = lambda event_type, payload: {
        "type": event_type,
        "payload": payload,
    }
    return message_stream_writer


@pytest.fixture()
def activity_runtime(activity_writer: AsyncMock) -> ActivityRuntime:
    return ActivityRuntime(activity_writer, ActivityHandlerRegistry())


@pytest.mark.asyncio
async def test_default_activity_handler_keeps_safe_fact_without_detail(
    activity_runtime: ActivityRuntime,
) -> None:
    event = await activity_runtime.started(
        activity_id="activity_1",
        kind="provider.private_operation",
        summary="处理中",
        detail={"secret": "must_not_be_projected"},
    )

    payload = event["payload"]
    assert isinstance(payload, dict)
    assert payload["activity_id"] == "activity_1"
    assert payload["detail_available"] is False
    assert "detail" not in payload


@pytest.mark.asyncio
@pytest.mark.parametrize("native_path", ["/private-activity/error.json", r"C:\private-activity\error.json", r"\\server\private-activity\error.json"])
async def test_activity_handler_failure_degrades_detail_only(
    activity_writer: AsyncMock,
    native_path: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    activity_writer.commit.side_effect = lambda _event_type, payload: payload
    error = PermissionError(13, f"不能读取 {native_path}", native_path)

    class BrokenHandler:
        def normalize(self, payload: Mapping[str, object]) -> Mapping[str, object]:
            raise error

        def snapshot_detail(self, payload: Mapping[str, object]) -> None:
            return None

        def can_confirm_stop(self, payload: Mapping[str, object]) -> bool:
            return False

    activity_handler_registry = ActivityHandlerRegistry()
    activity_handler_registry.register(
        kind="browser.session",
        handler=BrokenHandler(),
        capabilities=ActivityCapabilities(detail=True),
    )
    custom_activity_runtime = ActivityRuntime(
        activity_writer, activity_handler_registry
    )

    payload = await custom_activity_runtime.updated(
        activity_id="activity_1",
        kind="browser.session",
        summary="浏览器仍在运行",
    )

    assert payload["activity_id"] == "activity_1"
    assert payload["detail_available"] is False
    assert payload["summary"] == "浏览器仍在运行"
    detail_error = payload["detail_error"]
    assert isinstance(detail_error, str)
    assert "PermissionError" in detail_error
    assert "errno=13" in detail_error
    assert native_path not in detail_error
    assert any(record.exc_info and record.exc_info[1] is error for record in caplog.records)


@pytest.mark.asyncio
async def test_known_activity_handler_only_projects_whitelisted_detail(
    activity_writer: AsyncMock,
    activity_runtime: ActivityRuntime,
) -> None:
    activity_writer.commit.side_effect = lambda _event_type, payload: payload

    payload = await activity_runtime.started(
        activity_id="activity_1",
        kind="approval.wait",
        detail={
            "approval_id": "approval_1",
            "required_action": "confirm",
            "secret_prompt": "不要投影",
        },
    )

    assert payload["detail"] == {
        "approval_id": "approval_1",
        "required_action": "confirm",
    }
    assert payload["detail_available"] is True
