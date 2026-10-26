"""测试显式完整成员合同和唯一进程句柄，不推断共享关系。"""

from __future__ import annotations

from dataclasses import dataclass, field
from uuid import uuid4

from app.gateway.registry import GatewayWorkspaceRegistry, WorkspaceTarget
from app.gateway.runtime.managed_backend import (
    ManagedBackendContract,
    ManagedBackendMember,
    ManagedBackendOwner,
)
from app.gateway.runtime.process_lifecycle import ManagedProcessHandle
from app.gateway.runtime.workspace import WorkspaceRuntime
from app.schemas.gateway.lifecycle import GatewayRuntimeGroupRequest


@dataclass
class ProcessProbe:
    name: str = "backend"
    events: list[str] = field(default_factory=list)
    alive: bool = True
    closed: bool = False
    detached: bool = False
    terminate_requested: bool = False
    failure: Exception | None = None
    process: object = field(default_factory=object)
    pid: int = 100_000

    def is_alive(self) -> bool:
        return self.alive

    def request_terminate(self) -> None:
        self.events.append(f"terminate:{self.name}")
        self.terminate_requested = True

    def close(self, *, timeout_seconds: float = 8) -> None:
        assert timeout_seconds > 0
        self.events.append(f"close:{self.name}")
        if self.failure is not None:
            raise self.failure
        self.alive = False
        self.closed = True

    def detach(self) -> None:
        self.events.append(f"detach:{self.name}")
        self.detached = True


@dataclass
class ManagedBackendGraph:
    owner: ManagedBackendOwner
    runtimes: dict[str, WorkspaceRuntime]
    targets: tuple[WorkspaceTarget, ...]

    @property
    def group_request(self) -> GatewayRuntimeGroupRequest:
        return GatewayRuntimeGroupRequest(
            expected_backend_generation=self.owner.contract.generation,
            expected_workspace_ids=list(self.owner.contract.workspace_ids),
        )

    def publish(self, registry: GatewayWorkspaceRegistry) -> None:
        registry.publish_backend_runtime(self.owner, self.owner.contract, self.runtimes)


def backend_graph(
    targets: tuple[WorkspaceTarget, ...], *,
    backend_url: str,
    process: ManagedProcessHandle | None = None,
    auxiliary_processes: dict[str, dict[str, ManagedProcessHandle]] | None = None,
    generation: str | None = None,
    owner_id: str | None = None,
) -> ManagedBackendGraph:
    members = []
    runtimes = {}
    for index, target in enumerate(targets):
        if target.backend_workspace_id is None:
            raise ValueError("测试完整合同必须明确 Backend UUID")
        services = {
            "terminal_manager": target.local_service_urls.get("terminal_manager", f"http://127.0.0.1:{42001 + index * 2}"),
            "browser_manager": target.local_service_urls.get("browser_manager", f"http://127.0.0.1:{42002 + index * 2}"),
        }
        members.append(ManagedBackendMember(target.workspace_id, target.backend_workspace_id))
        runtimes[target.workspace_id] = WorkspaceRuntime(
            service_urls={"workspace_api": backend_url, **services},
            processes=dict((auxiliary_processes or {}).get(target.workspace_id, {})),
            backend_workspace_id=target.backend_workspace_id,
        )
    owner = ManagedBackendOwner(ManagedBackendContract(
        owner_id or f"backend_owner_{uuid4().hex}",
        generation or f"backend_generation_{uuid4().hex}",
        tuple(members), backend_url,
    ))
    if process is not None:
        owner.set_process(process)
    for runtime in runtimes.values():
        runtime.backend_owner = owner
    return ManagedBackendGraph(owner, runtimes, targets)
