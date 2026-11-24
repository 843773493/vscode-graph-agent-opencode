from __future__ import annotations

from collections.abc import Callable, Iterator
from functools import partial
from copy import deepcopy
import json
from types import SimpleNamespace
from pathlib import Path

import httpx
import pytest

from app.agents.tool_invocation_context import (
    ThreadRuntimeBinding,
    ToolInvocationContext,
)
from app.core.path_utils import workspace_context
from app.runtime.network_proxy import ProxyRoutes
from app.services.infrastructure.config import ConfigSnapshot, build_config_snapshot
from app.services.infrastructure.config_service import ConfigService
from configs.inheritance import ConfigGraph
from configs.network import DirectProxy
from app.core.workspace_identity import load_or_create_workspace_id
from app.domain.itemized.identity.item_projection import ItemProjectionIdentity
from tests.harness.python.run_context import TestRunContext
from tests.support.communication.probe import (
    SOURCE_SESSION,
    SOURCE_THREAD,
    CommunicationProbe,
)
from tests.support.workspaces import (
    prepare_default_test_workspace,
    prepare_test_workspace,
)


@pytest.fixture
def workspace_scope(request: pytest.FixtureRequest) -> Iterator[tuple[str, Path]]:
    """需要 workspace 上下文的单测显式启用；不给无归属调用补默认值。"""
    yield from _workspace_scope(request, template_items=None)


@pytest.fixture
def empty_workspace_scope(
    request: pytest.FixtureRequest,
) -> Iterator[tuple[str, Path]]:
    """创建带 identity/context 的空 workspace，不复制模板项目文件。"""
    yield from _workspace_scope(request, template_items=())


def _workspace_scope(
    request: pytest.FixtureRequest,
    *,
    template_items: tuple[str, ...] | None,
) -> Iterator[tuple[str, Path]]:
    context = TestRunContext.from_test_file(Path(request.node.path))
    root = context.runtime_root_for_node(request.node.nodeid) / "workspace"
    template_root = Path.cwd() / "tests/fixtures/workspaces/default_test_workspace"
    if template_items is None:
        prepare_default_test_workspace(
            workspace_root=root,
            template_root=template_root,
        )
    else:
        prepare_test_workspace(
            workspace_root=root,
            template_root=template_root,
            template_items=template_items,
        )
    workspace_id = load_or_create_workspace_id(root)
    with workspace_context(workspace_id, root):
        yield workspace_id, root


class _TestSessionLifecycleGuard:
    def __init__(self) -> None:
        self.checked_session_ids: list[str] = []

    def __call__(self, session_id: str) -> None:
        prefix, separator, identity = session_id.partition("_")
        if prefix not in {"ses", "session"} or not separator or not identity:
            raise AssertionError(f"测试传入了无效的伪 Session identity: {session_id!r}")
        self.checked_session_ids.append(session_id)


@pytest.fixture
def session_lifecycle_guard() -> _TestSessionLifecycleGuard:
    """校验并记录单元测试使用的伪 Session identity。"""
    return _TestSessionLifecycleGuard()


@pytest.fixture
def communication_probe() -> CommunicationProbe:
    return CommunicationProbe()


@pytest.fixture
def communication_invocation() -> Iterator[ToolInvocationContext]:
    context = ToolInvocationContext(thread_binding=ThreadRuntimeBinding(SOURCE_SESSION, SOURCE_THREAD))
    token = context.set_tool_call_id("call-communication-test", projection_identity=ItemProjectionIdentity(model_call_id="019c390d-6a70-7c85-8709-afddc785bf93"))
    yield context
    context.reset_tool_call_id(token)


@pytest.fixture
def resource_http(monkeypatch: pytest.MonkeyPatch) -> Callable[[Callable[[httpx.Request], httpx.Response]], None]:
    """两类资源客户端共用正式 HTTPX 编解码，仅替换单次网络边界。"""
    def install(handler: Callable[[httpx.Request], httpx.Response]) -> None:
        client = partial(httpx.AsyncClient, transport=httpx.MockTransport(handler))
        monkeypatch.setattr("app.services.infrastructure.terminal_manager_client.AsyncClient", client)
        monkeypatch.setattr("app.services.infrastructure.browser_manager_client.AsyncClient", client)

    return install


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
