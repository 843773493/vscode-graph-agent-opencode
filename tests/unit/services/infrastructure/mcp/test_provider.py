"""动态来源只保存引用，离线启动和配置替换不借缓存 launch。"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.core.lifecycle import LifetimeScope
from app.services.infrastructure.external_resource_leases import (
    ExternalResourceLeaseLedger,
)
from app.services.infrastructure.mcp import McpCatalogOwner
from app.services.infrastructure.mcp.config import parse_mcp_server_configs


@pytest.fixture
def provider_reference() -> dict[str, str]:
    return {"gateway_id": "gateway_" + "a" * 32, "backend_id": "00000000-0000-4000-8000-000000000001",
        "client_id": "original-client", "view_id": "default", "host_generation": "original-generation", "host_kind": "node",
        "extension_id": "probe.dynamic", "extension_key": "probe.dynamic-1.0.0", "collection_id": "probe.dynamic/definitions",
        "provider_ref": "original-provider", "definition_ref": "original-object"}


@pytest.fixture
def provider_config(provider_reference: dict[str, str]) -> dict[str, object]:
    return {"servers": {"dynamic": {"enabled": True, "transport": "stdio", "provider_definition": provider_reference}}}


@pytest.mark.parametrize("field", ["command", "url", "env", "headers", "managed_launch"])
def test_reference_cannot_persist_executable_or_secret_fields(provider_reference: dict[str, str], tmp_path: Path, field: str) -> None:
    with pytest.raises(ValueError, match="不支持的字段"):
        parse_mcp_server_configs({"servers": {"dynamic": {"transport": "stdio", "provider_definition": provider_reference,
            field: "must-not-persist"}}}, workspace_root=tmp_path)


def test_web_provider_stdio_has_no_native_execution_context(provider_reference: dict[str, str], tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="Web Provider"):
        parse_mcp_server_configs({"servers": {"dynamic": {"transport": "stdio",
            "provider_definition": {**provider_reference, "host_kind": "web"}}}}, workspace_root=tmp_path)


async def test_offline_cold_start_preserves_reference_and_reports_unavailable(provider_config, provider_reference, tmp_path, event_channel_service) -> None:
    ledger = ExternalResourceLeaseLedger()
    owner = McpCatalogOwner(raw_config=provider_config, workspace_root=tmp_path, event_service=event_channel_service, leases=ledger)
    await owner.start()
    try:
        server, = owner.list_servers()
        assert server.status == "unavailable" and server.unavailable_reason
        assert server.provider_definition.model_dump() == provider_reference
        assert owner.get_tool_ids() == set() and server.connection_ref is None
        assert all(item.status == "stopped" for item in ledger.records_by_kind("mcp_connection"))
        assert owner.server_configuration("dynamic").connection is None
    finally:
        await owner.shutdown()


async def test_offline_source_save_can_publish_configuration_without_starting_renderer(provider_config, tmp_path, event_channel_service) -> None:
    owner = McpCatalogOwner(raw_config={}, workspace_root=tmp_path, event_service=event_channel_service, leases=ExternalResourceLeaseLedger())
    shadow = LifetimeScope("provider-config-test")
    await owner.start()
    try:
        publish, _ = await owner.prepare_config(provider_config, shadow, lambda _effect: None)
        publish()
        await shadow.close()
        server, = owner.list_servers()
        assert server.status == "unavailable" and not server.tools
        assert owner.server_configuration("dynamic").provider_definition is not None
    finally:
        await shadow.close()
        await owner.shutdown()
