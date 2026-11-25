from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.runtime.network_proxy import ProxyRoutes
from app.services.infrastructure.config import ConfigSnapshot, build_config_snapshot
from app.services.infrastructure.config_service import ConfigService
from configs.inheritance import ConfigGraph
from configs.network import DirectProxy


@pytest.fixture
def direct_proxy_routes() -> ProxyRoutes:
    """模型单测显式使用直连出口，避免读取宿主机代理配置。"""
    return ProxyRoutes.from_configuration(DirectProxy())


@pytest.fixture
def provider_with_direct_network(direct_proxy_routes: ProxyRoutes):
    """准备已经完成网络解析的 Provider 输入。"""
    return {
        "network": {"proxy": {"mode": "direct"}},
        "proxy_environment": {},
        "proxy_routes": direct_proxy_routes,
    }


@pytest.fixture
def workspace_config_snapshots(tmp_path: Path):
    """以正式 v4 Workspace 配置和来源图构造可变业务快照。"""
    service = ConfigService(
        config_dir=Path.cwd() / "configs",
        config_path=tmp_path / "workspace.jsonc",
    )
    base = service.get_snapshot()

    def build(
        payload: dict[str, object] | None = None,
        *,
        graph: ConfigGraph | None = None,
    ) -> ConfigSnapshot:
        selected_graph = graph or base.graph
        selected_graph.resolve_scope()
        return build_config_snapshot(
            deepcopy(base.to_dict() if payload is None else payload),
            graph=selected_graph,
            source_paths=tuple(selected_graph.source_locations.values()),
            schema_path=base.schema_path,
        )

    def changed() -> ConfigSnapshot:
        payload = base.to_dict()
        payload["context"]["source_messages"]["grouping"] = "separate"
        return build(payload)

    def service_for_document(document: dict[str, object], name: str) -> ConfigService:
        config_path = tmp_path / name
        config_path.write_text(json.dumps(document), encoding="utf-8")
        return ConfigService(config_dir=Path.cwd() / "configs", config_path=config_path)

    return SimpleNamespace(base=base, build=build, changed=changed, service_for_document=service_for_document)
