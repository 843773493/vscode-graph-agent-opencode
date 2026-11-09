"""Node Debug run claim 与资源 lease 的 owner 投影。"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.services.infrastructure.external_resource_leases import (
    ExternalResourceLeaseLedger,
)
from app.services.infrastructure.node_debug.process.claim_runtime import (
    NodeDebugClaimRuntime,
    NodeDebugProcessLeaseIdentity,
)
from app.services.infrastructure.node_debug.process.launch_claim import (
    claim_with_target_pid,
    new_launch_claim,
)
from app.services.infrastructure.node_debug.runtime_state import NodeDebugRuntime
from app.services.infrastructure.node_debug.session.session_store import (
    NodeDebugSessionStore,
)
from tests.support.node_debug_dependencies import node_debug_binding_snapshot
from tests.unit.core.catalog_workspace_helper import build_catalog_workspace


@pytest.mark.asyncio
async def test_plugin_run_claim_owns_lease_until_plugin_exit_is_confirmed(
    tmp_path: Path,
) -> None:
    with build_catalog_workspace(tmp_path / "workspace") as workspace:
        session_id = await workspace.create_session("Node Debug lease")
        thread_id = workspace.resolver.main_thread_id(session_id)
        binding = node_debug_binding_snapshot().model_copy(
            update={"workspace_id": workspace.workspace_id}
        )
        claim = new_launch_claim(
            session_id=session_id,
            thread_id=thread_id,
            run_id="op_debug_lease_test",
            configuration_id="dbgcfg_" + "1" * 32,
            debugger_binding=binding,
            script_path="entry.mjs",
            args=[],
            node_executable="node",
            working_directory="",
            launch_profile_name=None,
            breakpoints=[],
        )
        store = NodeDebugSessionStore(workspace.resolver)
        leases = ExternalResourceLeaseLedger()
        runtime = NodeDebugRuntime(
            session_id=session_id,
            thread_id=thread_id,
            configuration_id=claim.configuration_id,
            debugger_binding=binding,
            workspace_root=workspace.workspace_root,
            script_path=workspace.workspace_root / "entry.mjs",
            relative_script_path="entry.mjs",
        )
        runtime.process_instance_id = claim.process_instance_id
        claims = NodeDebugClaimRuntime(
            session_store=store,
            external_resource_leases=leases,
            runtimes={(session_id, thread_id): runtime},
            residency_tracker=None,
            state_events=None,
        )

        await claims.write_launch_claim(claim)
        await claims.write_launch_claim(claim_with_target_pid(claim, pid=4242))
        await claims.mark_claim_running(runtime)

        identity = NodeDebugProcessLeaseIdentity.for_process_instance(
            session_id=session_id,
            thread_id=thread_id,
            process_instance_id=claim.process_instance_id,
        )
        active = leases.get_lease(identity.lease_id)
        assert active is not None and active.status == "active"
        assert store.read_launch_claim(session_id, thread_id).phase == "running"

        await claims.mark_claim_phase(runtime, "settled", "插件确认目标退出")
        settled = leases.get_lease(identity.lease_id)
        assert settled is not None and settled.status == "settled"
        assert store.read_launch_claim(session_id, thread_id).phase == "settled"
