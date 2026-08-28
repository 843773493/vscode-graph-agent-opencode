from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.api.node_debug import (
    activate_node_debug_configuration,
    copy_node_debug_configuration,
    create_node_debug_configuration,
    get_node_debug_capabilities,
    start_node_debug,
)
from app.schemas.internal_v2.node_debug import (
    NodeDebugCapabilitiesDTO,
    NodeDebugConfigurationActivateRequest,
    NodeDebugConfigurationCopyRequest,
    NodeDebugConfigurationCreateRequest,
    NodeDebugConfigurationDTO,
    NodeDebugLaunchProfileDTO,
    NodeDebugStartRequest,
    NodeDebugStateDTO,
)
from app.services.infrastructure.node_debug_service import NodeDebugService


@pytest.fixture
def node_debug_service() -> MagicMock:
    service = MagicMock(spec=NodeDebugService)
    service.get_capabilities.return_value = NodeDebugCapabilitiesDTO(
        enabled=True,
        default_adapter="node_inspector",
        supported_adapters=["node_inspector"],
        launch_profiles=[
            NodeDebugLaunchProfileDTO(
                name="node-test",
                adapter="node_inspector",
                runtime="node",
                supported=True,
            )
        ],
    )
    service.start = AsyncMock(
        return_value=NodeDebugStateDTO(
            session_id="ses_test",
            status="running",
            script_path="fixtures/debug.mjs",
            launch_profile_name="node-test",
            working_directory="fixtures",
        )
    )
    service.create_configuration = AsyncMock(
        return_value=NodeDebugStateDTO(
            session_id="ses_test",
            status="idle",
            active_configuration_id="dbgcfg_11111111111111111111111111111111",
        )
    )
    service.activate_configuration = AsyncMock(
        return_value=NodeDebugStateDTO(session_id="ses_test", status="idle")
    )
    service.copy_configuration = AsyncMock(
        return_value=NodeDebugConfigurationDTO(
            configuration_id="dbgcfg_22222222222222222222222222222222",
            name="复制方案",
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
    )
    return service


@pytest.mark.asyncio
async def test_capabilities_endpoint_returns_safe_profiles(
    node_debug_service: MagicMock,
) -> None:
    response = await get_node_debug_capabilities(
        _="local-token",
        request_id="req_capabilities",
        node_debug_service=node_debug_service,
    )

    assert response.request_id == "req_capabilities"
    assert response.data is not None
    assert response.data.launch_profiles[0].name == "node-test"


@pytest.mark.asyncio
async def test_start_endpoint_forwards_profile_and_working_directory(
    node_debug_service: MagicMock,
) -> None:
    payload = NodeDebugStartRequest(
        session_id="ses_test",
        path="fixtures/debug.mjs",
        working_directory="fixtures",
        launch_profile_name="node-test",
        args=["23"],
    )

    response = await start_node_debug(
        payload=payload,
        _="local-token",
        request_id="req_start",
        node_debug_service=node_debug_service,
    )

    assert response.request_id == "req_start"
    node_debug_service.start.assert_awaited_once_with(
        session_id="ses_test",
        configuration_id=None,
        path="fixtures/debug.mjs",
        args=["23"],
        breakpoints=[],
        launch_profile_name="node-test",
        working_directory="fixtures",
    )


@pytest.mark.asyncio
async def test_configuration_endpoints_forward_session_and_portable_payload(
    node_debug_service: MagicMock,
) -> None:
    create_payload = NodeDebugConfigurationCreateRequest(
        session_id="ses_test",
        name="测试方案",
        script_path="fixtures/debug.mjs",
    )
    created = await create_node_debug_configuration(
        payload=create_payload,
        _="local-token",
        request_id="req_create",
        node_debug_service=node_debug_service,
    )
    assert created.data is not None
    node_debug_service.create_configuration.assert_awaited_once_with(create_payload)

    configuration_id = "dbgcfg_11111111111111111111111111111111"
    await activate_node_debug_configuration(
        configuration_id=configuration_id,
        payload=NodeDebugConfigurationActivateRequest(session_id="ses_test"),
        _="local-token",
        request_id="req_activate",
        node_debug_service=node_debug_service,
    )
    node_debug_service.activate_configuration.assert_awaited_once_with(
        "ses_test",
        configuration_id,
    )

    copied = await copy_node_debug_configuration(
        configuration_id=configuration_id,
        payload=NodeDebugConfigurationCopyRequest(
            source_session_id="ses_test",
            target_session_id="ses_target",
        ),
        _="local-token",
        request_id="req_copy",
        node_debug_service=node_debug_service,
    )
    assert copied.data is not None
    assert "session_id" not in copied.data.model_dump()
