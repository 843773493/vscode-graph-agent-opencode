"""Node Debug 调试方案配置控制链路的守卫与导入分支单元测试。

补齐 NodeDebugConfigurationControlMixin 中未被既有用例触达的分支：
import_configuration 的激活收口，以及 _assert_no_running_target /
_assert_configuration_not_running 两条运行中阻断断言。直接注入在册 runtime，
不启动真实 Node 进程。
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.schemas.internal_v2.node_debug import (
    NodeDebugConfigurationBreakpointDTO,
    NodeDebugConfigurationCreateRequest,
    NodeDebugConfigurationDTO,
    NodeDebugConfigurationUpdateRequest,
)
from app.services.infrastructure.config_service import ConfigService
from app.services.infrastructure.external_resource_leases import (
    ExternalResourceLeaseLedger,
)
from app.services.infrastructure.node_debug.runtime_state import NodeDebugRuntime
from app.services.infrastructure.node_debug.service import NodeDebugService
from app.services.infrastructure.node_debug.session.session_store import (
    NodeDebugSessionStore,
)
from tests.support.node_debug_dependencies import (
    permissive_node_debug_session_admission,
)

_SESSION_ID = "ses_00000000000000000000000000000011"
_OTHER_SESSION_ID = "ses_00000000000000000000000000000012"


class _SessionCatalogResolverStub:
    def __init__(self, session_root: Path) -> None:
        self._session_root = session_root

    def resolve_session_node(self, session_id: str) -> Path:
        path = self._session_root / session_id
        path.mkdir(parents=True, exist_ok=True)
        return path

    def resolve_thread_node(self, session_id: str, thread_id: str) -> Path:
        path = self._session_root / session_id / "threads" / thread_id
        path.mkdir(parents=True, exist_ok=True)
        return path


def _service(tmp_path: Path) -> NodeDebugService:
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir(exist_ok=True)
    (workspace_root / "entry.mjs").write_text(
        "const value = 1;\nconsole.log(value);\nexport {};\n",
        encoding="utf-8",
    )
    return NodeDebugService(
        workspace_root=workspace_root,
        config_service=ConfigService(workspace_root=workspace_root),
        session_store=NodeDebugSessionStore(
            _SessionCatalogResolverStub(tmp_path / "sessions")
        ),
        session_admission=permissive_node_debug_session_admission(),
        external_resource_leases=ExternalResourceLeaseLedger(),
    )


def _running_runtime(
    service: NodeDebugService,
    *,
    session_id: str,
    configuration_id: str,
) -> NodeDebugRuntime:
    runtime = NodeDebugRuntime(
        session_id=session_id,
        thread_id="main",
        configuration_id=configuration_id,
        workspace_root=service._workspace_root,
        script_path=service._workspace_root / "entry.mjs",
        relative_script_path="entry.mjs",
        status="running",
    )
    service._runtimes[(session_id, "main")] = runtime
    return runtime


def _portable_configuration(name: str = "可导入方案") -> NodeDebugConfigurationDTO:
    now = datetime.now(UTC)
    return NodeDebugConfigurationDTO(
        configuration_id="dbgcfg_" + "a" * 32,
        name=name,
        script_path="entry.mjs",
        breakpoints=[
            NodeDebugConfigurationBreakpointDTO(
                breakpoint_id="node-bp-imported",
                path="entry.mjs",
                line=1,
                original_line=1,
                created_at=now,
            )
        ],
        created_at=now,
        updated_at=now,
    )


@pytest.mark.asyncio
async def test_import_configuration_without_activate_leaves_nothing_active(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    state = await service.import_configuration(
        _SESSION_ID,
        _portable_configuration(),
        thread_id="main",
        activate=False,
    )
    assert [item.name for item in state.configurations] == ["可导入方案"]
    assert state.active_configuration_id is None
    assert state.active_configuration_name is None
    # 动作文案经 append_pending_action 收口；断言全文以固定这条唯一实现。
    assert [item.message for item in state.actions] == [
        "已导入调试方案 可导入方案"
    ]


@pytest.mark.asyncio
async def test_import_configuration_activate_syncs_active_selection(
    tmp_path: Path,
) -> None:
    """激活导入必须同步活动方案并在已加载会话内生效（杀死漏掉收口的变异）。"""
    service = _service(tmp_path)
    state = await service.import_configuration(
        _SESSION_ID,
        _portable_configuration("激活方案"),
        thread_id="main",
        activate=True,
    )
    assert state.active_configuration_name == "激活方案"
    assert state.active_configuration_id == "dbgcfg_" + "a" * 32
    assert state.script_path == "entry.mjs"
    # 激活导入必须把方案断点同步为待安装断点；漏掉 sync_active_configuration
    # 时这里会得到空断点，变异因此变红。
    assert [(item.path, item.line) for item in state.breakpoints] == [("entry.mjs", 1)]
    reloaded = await service.get_state(_SESSION_ID, "main")
    assert reloaded.active_configuration_name == "激活方案"
    assert [(item.path, item.line) for item in reloaded.breakpoints] == [
        ("entry.mjs", 1)
    ]


@pytest.mark.asyncio
async def test_create_configuration_activate_is_blocked_while_target_runs(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    created = await service.create_configuration(
        NodeDebugConfigurationCreateRequest(
            session_id=_SESSION_ID,
            thread_id="main",
            name="运行中方案",
            script_path="entry.mjs",
        )
    )
    configuration_id = created.active_configuration_id
    assert configuration_id is not None
    _running_runtime(
        service, session_id=_SESSION_ID, configuration_id=configuration_id
    )

    with pytest.raises(RuntimeError, match="目标程序运行中，停止后才能切换调试方案"):
        await service.create_configuration(
            NodeDebugConfigurationCreateRequest(
                session_id=_SESSION_ID,
                thread_id="main",
                name="被阻断方案",
                script_path="entry.mjs",
                activate=True,
            )
        )


@pytest.mark.asyncio
async def test_update_and_delete_running_configuration_are_blocked(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    created = await service.create_configuration(
        NodeDebugConfigurationCreateRequest(
            session_id=_SESSION_ID,
            thread_id="main",
            name="运行中方案",
            script_path="entry.mjs",
        )
    )
    configuration_id = created.active_configuration_id
    assert configuration_id is not None
    _running_runtime(
        service, session_id=_SESSION_ID, configuration_id=configuration_id
    )

    with pytest.raises(RuntimeError, match="目标程序运行中，不能修改或删除当前调试方案"):
        await service.update_configuration(
            configuration_id,
            NodeDebugConfigurationUpdateRequest(
                session_id=_SESSION_ID,
                thread_id="main",
                name="改名失败",
                script_path="entry.mjs",
            ),
        )

    with pytest.raises(RuntimeError, match="目标程序运行中，不能修改或删除当前调试方案"):
        await service.delete_configuration(
            _SESSION_ID, configuration_id, thread_id="main"
        )


@pytest.mark.asyncio
async def test_copy_configuration_activate_is_blocked_while_target_runs(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    source = await service.create_configuration(
        NodeDebugConfigurationCreateRequest(
            session_id=_SESSION_ID,
            thread_id="main",
            name="来源方案",
            script_path="entry.mjs",
        )
    )
    source_id = source.active_configuration_id
    assert source_id is not None
    _running_runtime(
        service,
        session_id=_OTHER_SESSION_ID,
        configuration_id="dbgcfg_" + "b" * 32,
    )

    with pytest.raises(RuntimeError, match="目标程序运行中，停止后才能切换调试方案"):
        await service.copy_configuration(
            source_session_id=_SESSION_ID,
            target_session_id=_OTHER_SESSION_ID,
            configuration_id=source_id,
            source_thread_id="main",
            target_thread_id="main",
            activate=True,
        )
