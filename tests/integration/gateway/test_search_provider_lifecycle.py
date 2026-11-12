"""真实 Gateway 与 A/B Backend 的受控生命周期 Integration；不冒称无替身 E2E。"""

from __future__ import annotations

import asyncio
import json
from collections import Counter
from collections.abc import Iterator
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path

import httpx
import portalocker
import pytest

from app.core.session_lifecycle_gate import SessionLifecycleGate
from app.gateway.control.gateway_state import GatewayStateStore
from app.gateway.credentials import load_or_create_gateway_id
from app.gateway.registry import GatewayWorkspaceRegistry, WorkspaceTarget
from app.schemas.search_federation import MAX_SEARCH_OUTPUT_BYTES
from tests.harness.python.run_context import TestRunContext
from tests.integration.backend.sessions.test_runtime_source_pipeline import (
    RestartRuntimeSourceCase,
    _job,
    _prepare_source_workspace,
    _send,
)
from tests.support.api_helpers import create_session
from tests.support.e2e_control.harness import E2EProcessLease
from tests.support.model_stream.config import ModelStreamReplay, model_stream_replay
from tests.support.ports import integration_port_block_for_file
from tests.support.processes.gateway import (
    LOCAL_TOKEN_HEADERS,
    close_gateway_process,
    start_gateway_process,
)
from tests.support.processes.workspace import (
    BackendProcess,
    close_backend_process,
    start_backend_process,
    workspace_backend_id_for_root,
)
from tests.support.workspaces import (
    install_test_workspace_config,
    prepare_default_test_workspace,
)


@dataclass
class LifecycleNetwork:
    workspaces: tuple[Path, Path]
    backends: tuple[BackendProcess, BackendProcess]
    gateway_id: str
    gateway_port: int
    artifacts: Path
    control: Path
    model: ModelStreamReplay
    command_sequence: int = 0

    async def command(self, action: str, label: str, **parameters: str) -> dict:
        self.command_sequence += 1
        path = self.control / f"command-{self.command_sequence}.json"
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps({"action": action, "label": label, **parameters}) + "\n", encoding="utf-8")
        temporary.replace(path)
        reply = self.control / f"reply-{self.command_sequence}.json"
        async with asyncio.timeout(5):
            while not reply.exists():
                await asyncio.sleep(0.02)
        return json.loads(reply.read_text(encoding="utf-8"))

    def events(self) -> list[dict]:
        return [json.loads(line) for line in (self.control / "events.jsonl").read_text(encoding="utf-8").splitlines()]

    async def wait_event(self, event: str, label: str) -> dict:
        async with asyncio.timeout(10):
            while True:
                found = [item for item in self.events() if item["event"] == event and item.get("label") == label]
                if found:
                    result, = found
                    return result
                await asyncio.sleep(0.02)


@pytest.fixture
def lifecycle_network(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> Iterator[LifecycleNetwork]:
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    runtime = context.runtime_root_for_node(request.node.nodeid)
    ports = integration_port_block_for_file(Path(request.node.path))
    monkeypatch.setenv("BOXTEAM_GATEWAY_URL", f"http://127.0.0.1:{ports.port(20)}")
    workspace_a = _prepare_source_workspace(context, ports.port(10))
    workspace_b = workspace_a / "secondary-b"
    prepare_default_test_workspace(workspace_root=workspace_b,
                                   template_root=Path.cwd() / "tests/fixtures/workspaces/default_test_workspace",
                                   fresh_identity=True)
    install_test_workspace_config(workspace_root=workspace_b,
                                  config_path=workspace_a / ".boxteam/workspace.jsonc",
                                  schema_path=workspace_a / ".boxteam/workspace_schema.jsonc")
    home = runtime / "gateway-home"
    gateway_root = home / "state/gateway"
    gateway_id = load_or_create_gateway_id(gateway_root / "identity.json")
    token = gateway_root / "credentials/local-token"
    token.parent.mkdir(parents=True, exist_ok=True)
    token.write_text(LOCAL_TOKEN_HEADERS["X-Local-Token"] + "\n", encoding="utf-8")
    token.chmod(0o600)
    control = runtime / "search-lifecycle-control"
    control.mkdir(parents=True)
    with ExitStack() as cleanup:
        lease = E2EProcessLease(runtime / "service-ports.json", tuple(ports.port(offset) for offset in (0, 1, 20)))
        lease.acquire()
        cleanup.callback(lease.release)
        model = cleanup.enter_context(model_stream_replay(
            context.artifacts_dir, runtime_root=runtime,
            business_assertion="gateway-canonical-provider-close-and-version-handover",
        ))
        backends = []
        for index, workspace in enumerate((workspace_a, workspace_b)):
            backend = start_backend_process(
                workspace_root=str(workspace), port=ports.port(index), log_name=f"lifecycle-backend-{index}",
                asgi_app="tests.integration.gateway.search_lifecycle_app:app" if index == 0 else "tests.support.e2e_control.backend_app:app",
                env_overrides={**model.environment, **({"BOXTEAM_SEARCH_LIFECYCLE_CONTROL": str(control)} if index == 0 else {})},
                boxteam_home=home, artifacts_dir=context.artifacts_dir,
            )
            cleanup.callback(close_backend_process, backend)
            backends.append(backend)
        state = GatewayStateStore(path=gateway_root / "gateway.sqlite")
        registry = GatewayWorkspaceRegistry(gateway_id=gateway_id, storage_path=gateway_root / "workspaces.json", state_store=state)
        try:
            registry.upsert(WorkspaceTarget(
                gateway_id=gateway_id, workspace_id=backends[1].workspace_id, name="Independent B",
                root_path=str(workspace_b), backend_url=f"http://127.0.0.1:{backends[1].port}",
                connection_kind="local", desired_running=False,
            ), activate=False)
        finally:
            registry.close()
            state.close()
        gateway = start_gateway_process(workspace_root=workspace_a, default_backend_url=f"http://127.0.0.1:{backends[0].port}",
                                        port=ports.port(20), boxteam_home=home, artifacts_dir=context.artifacts_dir)
        cleanup.callback(close_gateway_process, gateway)
        yield LifecycleNetwork((workspace_a, workspace_b), (backends[0], backends[1]), gateway_id,
                               gateway.port, context.artifacts_dir, control, model)


def can_take_session_exclusive(workspace: Path, session_id: str) -> bool:
    """从测试进程探测真实 Backend 持有的原 OS guard，不读取内部锁计数。"""
    path = SessionLifecycleGate(workspace / ".boxteam/sessions").gates_root / f"{session_id}.lock"
    with path.open("r+b") as stream:
        try:
            portalocker.lock(stream, portalocker.LOCK_EX | portalocker.LOCK_NB)
        except portalocker.AlreadyLocked:
            return False
        portalocker.unlock(stream)
        return True


def assert_terminal(events: list[dict], *, failures: dict[str, str], successes: set[str]) -> None:
    ends = [item for item in events if item["type"] == "end"]
    end, = ends
    assert events[0]["type"] == "scope" and events[-1] is end
    assert [item["sequence"] for item in events] == list(range(1, len(events) + 1))
    branches = {item["branch"]["owner"]["workspace_id"]: item for item in end["summary"]["branches"]}
    assert set(branches) == set(failures) | successes
    for workspace_id, code in failures.items():
        assert branches[workspace_id]["state"] == "failed" and branches[workspace_id]["code"] == code
        assert branches[workspace_id]["http_status"] == 400
        assert branches[workspace_id]["returned_results"] == 0 and branches[workspace_id]["cursor"] is None
        assert not any(item["type"] == "page" and item["page"]["workspace_id"] == workspace_id for item in events)
    for workspace_id in successes:
        assert branches[workspace_id]["state"] == "complete"
    assert end["summary"]["state"] == ("incomplete" if failures else "complete")


def assert_canonical_page(events: list[dict], workspace_id: str, session_id: str, version: int) -> None:
    page, = [item["page"] for item in events if item["type"] == "page" and item["page"]["workspace_id"] == workspace_id]
    assert (page["provider_id"], page["contract_version"]) == (
        "canonical-message" if version == 1 else "canonical-message-v2", version,
    )
    hit, = page["messages"]["items"]
    assert hit["thread"]["workspace_id"] == workspace_id and hit["thread"]["session_id"] == session_id
    assert hit["match"]["role"] == "assistant" and "lifecycle-canonical" in hit["match"]["preview"]
    assert hit["match"]["reference"]["category"] == "message"


@pytest.mark.asyncio
async def test_canonical_provider_close_and_version_handover(lifecycle_network: LifecycleNetwork) -> None:
    network = lifecycle_network
    workspace_a, workspace_b = (workspace_backend_id_for_root(root) for root in network.workspaces)
    owners = [{"gateway_id": network.gateway_id, "workspace_id": identity} for identity in (workspace_a, workspace_b)]
    query = {"category": "message", "operation": "content", "query": "lifecycle-canonical", "match_mode": "literal", "limit": 10}
    observations = []
    pending: set[asyncio.Task[list[dict]]] = set()
    sessions: list[str] = []
    async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{network.gateway_port}", headers=LOCAL_TOKEN_HEADERS,
                                 timeout=30, trust_env=False) as client:
        client.headers["X-BoxTeam-Target-Gateway-Id"] = network.gateway_id

        async def search(selected: list[dict], request_id: str, *, version: int = 1,
                         progress: list[dict] | None = None, b_ready: asyncio.Event | None = None) -> list[dict]:
            payload = {"scope": {"mode": "selected", "owners": selected}, "queries": [{**query, "contract_version": version}]}
            events = progress if progress is not None else []
            async with client.stream("POST", "/api/gateway/search", json=payload, headers={"X-Request-ID": request_id}) as response:
                assert response.status_code == 200, (await response.aread()).decode()
                assert response.headers["X-Request-ID"] == request_id
                async for line in response.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    event = json.loads(line.removeprefix("data: "))
                    assert event["request_id"] == request_id
                    events.append(event)
                    if b_ready is not None and event["type"] == "page" and event["page"]["workspace_id"] == workspace_b:
                        b_ready.set()
                assert response.num_bytes_downloaded <= MAX_SEARCH_OUTPUT_BYTES
            observations.append({"request_id": request_id, "payload": payload, "events": events})
            return events

        async def capabilities(workspace_id: str) -> list[dict]:
            response = await client.get("/api/v1/search/capabilities", headers={"X-BoxTeam-Workspace-Id": workspace_id})
            assert response.status_code == 200, response.text
            assert response.headers["X-Request-ID"] == response.json()["request_id"]
            return response.json()["data"]["providers"]

        def query_entries(provider: str) -> int:
            return sum(item["event"] == "query_enter" and item.get("provider_id") == provider for item in network.events())

        try:
            health = await client.get("/api/gateway/health")
            listing = await client.get("/api/gateway/workspaces", params={"check_health": False})
            for response in (health, listing):
                assert response.status_code == 200, response.text
                assert response.headers["X-Request-ID"] == response.json()["request_id"]
            assert health.json()["data"]["gateway_id"] == network.gateway_id
            assert {item["workspace_id"] for item in listing.json()["data"]["items"]} == {workspace_a, workspace_b}
            for index, workspace_id in enumerate((workspace_a, workspace_b)):
                client.headers["X-BoxTeam-Workspace-Id"] = workspace_id
                session = await create_session(client, f"Provider lifecycle {'AB'[index]}")
                case = RestartRuntimeSourceCase(client, network.model, network.workspaces[index], network.artifacts, [network.backends[index]])
                await case.plan(session, [{"content": f"lifecycle-canonical {'AB'[index]} 🧪"}])
                accepted = await _send(case, session, "保存真实 canonical 生命周期来源")
                job_id = accepted["job_id"]
                assert isinstance(job_id, str)
                await _job(case, job_id)
                sessions.append(session)
            session_a, session_b = sessions
            initial_a, initial_b = await capabilities(workspace_a), await capabilities(workspace_b)
            initial_ids = {item["provider_id"] for item in initial_a}
            baseline = await search(owners, "req_lifecycle_baseline")
            assert_terminal(baseline, failures={}, successes={workspace_a, workspace_b})
            for workspace_id, session in ((workspace_a, session_a), (workspace_b, session_b)):
                assert_canonical_page(baseline, workspace_id, session, 1)

            await network.command("arm", "handover", session_id=session_a)
            progress: list[dict] = []
            b_ready = asyncio.Event()
            inflight = asyncio.create_task(search(owners, "req_lifecycle_v1_inflight", progress=progress, b_ready=b_ready))
            pending.add(inflight)
            entered = await network.wait_event("read_worker_enter", "handover")
            assert entered["owner"] == "SessionTurnHistoryService.search_messages"
            async with asyncio.timeout(5):
                await b_ready.wait()
            assert_canonical_page(progress, workspace_b, session_b, 1)
            assert not inflight.done() and not can_take_session_exclusive(network.workspaces[0], session_a)
            closed = await network.command("close_registration", "handover")
            registration = closed["state"]["registrations"]["canonical-message"]
            assert registration == {"active": False, "tasks": 1}
            assert closed["state"]["closing"]["handover"] is False
            assert "canonical-message" not in {item["provider_id"] for item in await capabilities(workspace_a)}
            calls_before = query_entries("canonical-message")
            rejected = await search([owners[0]], "req_lifecycle_closed_admission")
            assert_terminal(rejected, failures={workspace_a: "provider_unavailable"}, successes=set())
            assert query_entries("canonical-message") == calls_before
            conflict = await network.command("register_v2", "too_early")
            assert conflict["error_type"] == "ValueError" and "类别／操作已注册" in conflict["message"]
            independent = await search([owners[1]], "req_lifecycle_b_during_handover")
            assert_terminal(independent, failures={}, successes={workspace_b})
            assert_canonical_page(independent, workspace_b, session_b, 1)
            assert not can_take_session_exclusive(network.workspaces[0], session_a) and not inflight.done()
            assert not any(item["event"] == "provider_released" and item.get("provider_id") == "canonical-message" for item in network.events())
            await network.command("release", "handover")
            await network.wait_event("close_complete", "handover")
            finished = await inflight
            pending.remove(inflight)
            assert_terminal(finished, failures={workspace_a: "provider_unavailable"}, successes={workspace_b})
            assert can_take_session_exclusive(network.workspaces[0], session_a)
            events = network.events()
            exited, = [item for item in events if item["event"] == "read_worker_exit" and item.get("label") == "handover"]
            cancelled, = [item for item in events if item["event"] == "query_exit" and item.get("provider_id") == "canonical-message" and item.get("error_type") == "CancelledError"]
            released, = [item for item in events if item["event"] == "provider_released" and item.get("provider_id") == "canonical-message"]
            complete, = [item for item in events if item["event"] == "close_complete" and item.get("label") == "handover"]
            assert entered["sequence"] < exited["sequence"] < cancelled["sequence"] < released["sequence"] < complete["sequence"]
            assert entered["thread"] != cancelled["thread"]

            registered = await network.command("register_v2", "after_settlement")
            assert "state" in registered
            versions = [item for item in await capabilities(workspace_a) if item["category"] == "message"]
            assert [(item["provider_id"], item["contract_version"]) for item in versions] == [("canonical-message-v2", 2)]
            v1_calls, v2_calls = query_entries("canonical-message"), query_entries("canonical-message-v2")
            stale = await search([owners[0]], "req_lifecycle_old_version")
            assert_terminal(stale, failures={workspace_a: "unsupported_version"}, successes=set())
            assert query_entries("canonical-message") == v1_calls and query_entries("canonical-message-v2") == v2_calls
            v2 = await search([owners[0]], "req_lifecycle_new_version", version=2)
            assert_terminal(v2, failures={}, successes={workspace_a})
            assert_canonical_page(v2, workspace_a, session_a, 2)
            assert query_entries("canonical-message") == v1_calls and query_entries("canonical-message-v2") == v2_calls + 1

            await network.command("arm", "shutdown", session_id=session_a)
            shutting_down = asyncio.create_task(search([owners[0]], "req_lifecycle_v2_inflight", version=2))
            pending.add(shutting_down)
            await network.wait_event("read_worker_enter", "shutdown")
            closing = await network.command("close_service", "shutdown")
            assert closing["state"]["active"] == [] and closing["state"]["closing"]["shutdown"] is False
            assert closing["state"]["registrations"]["canonical-message-v2"] == {"active": False, "tasks": 1}
            assert await capabilities(workspace_a) == [] and await capabilities(workspace_b) == initial_b
            calls_before = query_entries("canonical-message-v2")
            unavailable = await search([owners[0]], "req_lifecycle_service_closed", version=2)
            assert_terminal(unavailable, failures={workspace_a: "provider_unavailable"}, successes=set())
            assert query_entries("canonical-message-v2") == calls_before
            independent = await search([owners[1]], "req_lifecycle_b_during_shutdown")
            assert_terminal(independent, failures={}, successes={workspace_b})
            assert_canonical_page(independent, workspace_b, session_b, 1)
            assert not shutting_down.done() and not can_take_session_exclusive(network.workspaces[0], session_a)
            assert not any(item["event"] == "provider_released" and item.get("provider_id") == "canonical-message-v2" for item in network.events())
            await network.command("release", "shutdown")
            await network.wait_event("close_complete", "shutdown")
            assert_terminal(await shutting_down, failures={workspace_a: "provider_unavailable"}, successes=set())
            pending.remove(shutting_down)
            assert can_take_session_exclusive(network.workspaces[0], session_a)
            await network.command("close_service", "shutdown_again")
            await network.wait_event("close_complete", "shutdown_again")
            after_close = await network.command("register_v2", "service_already_closed")
            assert after_close["error_type"] == "SearchContractError" and after_close["code"] == "provider_unavailable"
            releases = Counter(item["provider_id"] for item in network.events() if item["event"] == "provider_released")
            assert releases == Counter({provider: 1 for provider in initial_ids | {"canonical-message-v2"}})
            events = network.events()
            worker_exit, = [item["sequence"] for item in events if item["event"] == "read_worker_exit" and item.get("label") == "shutdown"]
            v2_release, = [item["sequence"] for item in events if item["event"] == "provider_released" and item.get("provider_id") == "canonical-message-v2"]
            service_close, = [item["sequence"] for item in events if item["event"] == "close_complete" and item.get("label") == "shutdown"]
            assert worker_exit < v2_release < service_close
        finally:
            # 失败也先解除测试闸门，再结算本测试拥有的网络读取；不遗留 worker 阻塞清理。
            for label in ("handover", "shutdown"):
                events = network.events()
                if any(item["event"] == "read_worker_enter" and item.get("label") == label for item in events) and not any(
                    item["event"] == "read_release_requested" and item.get("label") == label for item in events
                ):
                    await network.command("release", label)
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            (network.artifacts / "provider-lifecycle-wire.json").write_text(json.dumps({
                "owners": owners, "sessions": sessions, "searches": observations,
                "controller_events": network.events(),
            }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
