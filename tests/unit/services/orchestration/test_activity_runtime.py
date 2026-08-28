from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from app.services.orchestration.activity_runtime import (
    ActivityCapabilities,
    ActivityHandlerRegistry,
    ActivityRuntime,
)


@pytest.mark.asyncio
async def test_default_activity_handler_keeps_safe_fact_without_detail() -> None:
    writer = AsyncMock()
    writer.commit.side_effect = lambda event_type, payload: {
        "type": event_type,
        "payload": payload,
    }
    runtime = ActivityRuntime(writer, ActivityHandlerRegistry())

    event = await runtime.started(
        activity_id="activity_1",
        kind="provider.private_operation",
        summary="处理中",
        detail={"secret": "must_not_be_projected"},
    )

    payload = event["payload"]
    assert payload["activity_id"] == "activity_1"
    assert payload["detail_available"] is False
    assert "detail" not in payload


@pytest.mark.asyncio
async def test_activity_handler_failure_degrades_detail_only() -> None:
    writer = AsyncMock()
    writer.commit.side_effect = lambda event_type, payload: payload

    class BrokenHandler:
        def normalize(self, payload):
            raise RuntimeError("detail unavailable")

        def snapshot_detail(self, payload):
            return None

        def can_confirm_stop(self, payload):
            return False

    registry = ActivityHandlerRegistry()
    registry.register(
        kind="browser.session",
        handler=BrokenHandler(),
        capabilities=ActivityCapabilities(detail=True),
    )
    runtime = ActivityRuntime(writer, registry)

    payload = await runtime.updated(
        activity_id="activity_1",
        kind="browser.session",
        summary="浏览器仍在运行",
    )

    assert payload["activity_id"] == "activity_1"
    assert payload["detail_available"] is False
    assert "detail_error" in payload


@pytest.mark.asyncio
async def test_known_activity_handler_only_projects_whitelisted_detail() -> None:
    writer = AsyncMock()
    writer.commit.side_effect = lambda event_type, payload: payload
    runtime = ActivityRuntime(writer, ActivityHandlerRegistry())

    payload = await runtime.started(
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


@pytest.mark.asyncio
async def test_activity_run_records_unknown_failure_before_reraising() -> None:
    writer = AsyncMock()
    writer.commit.side_effect = lambda event_type, payload: {
        "type": event_type,
        "payload": payload,
    }
    runtime = ActivityRuntime(writer, ActivityHandlerRegistry())

    async def fail() -> None:
        raise RuntimeError("外部资源状态未知")

    with pytest.raises(RuntimeError, match="外部资源状态未知"):
        await runtime.run(
            activity_id="activity_1",
            kind="resource.operation",
            operation=fail,
        )
    assert [call.args[0] for call in writer.commit.await_args_list] == [
        "activity.started",
        "activity.failed",
    ]
