"""候选应用必须保留当次消费者证明和对象，不能重新构造旧配置来回退。"""

from __future__ import annotations

import asyncio
import io
import json
import logging
from collections.abc import AsyncIterator, Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from app.core.logging_config import configure_application_logging, runtime_logging_state
from app.core.trace_middleware import TraceMiddleware
from app.gateway.auth import get_gateway_local_token
from app.gateway.config import GatewayConfigReloadService, load_gateway_config
from app.gateway.control.gateway_state import GatewayStateStore
from app.gateway.diagnostics import _LogCandidate, _logging_process_diagnostics
from app.gateway.federation.policy import FederationPolicyStore
from app.gateway.registry import GatewayWorkspaceRegistry, WorkspaceTarget
from app.gateway.routes.skill_sources import router as skill_source_router
from app.gateway.runtime.consumer_protocol import (
    GatewayRuntimeConsumerTransaction,
)
from app.gateway.runtime.logging_control import RuntimeLoggingProcessControl
from app.gateway.runtime.skill_sources import GatewaySkillSourceOwner
from app.gateway.runtime_proof import _apply_gateway_runtime_config
from app.schemas.runtime_logging import (
    RuntimeLoggingApply,
    RuntimeLoggingService,
    RuntimeLoggingSink,
    RuntimeLoggingStatus,
)
from app.services.infrastructure.config.state import ConfigConflictError
from app.services.infrastructure.resource_platform.observation.source_update_policy import (
    SourceUpdatePolicy,
)
from app.services.infrastructure.workspace_file_watch_service import (
    WorkspaceFileChangeBatch,
)
from tests.harness.python.run_context import TestRunContext
from tests.support.gateway_managed_backends import ProcessProbe


@dataclass
class SkillApplicationCase:
    config: ConfigApplicationCase
    owner: GatewaySkillSourceOwner
    client: httpx.AsyncClient
    skill: Path

    async def policy(self, mode: str, interval: str | None = None) -> None:
        self.config.path.write_text(json.dumps({
            "config_version": 3,
            "resources": {"skill_sources": {"local_updates": {"mode": mode, "poll_interval": interval}}},
        }))
        await self.config.service.reload()


async def _until(predicate: Callable[[], bool]) -> None:
    async with asyncio.timeout(3):
        while not predicate():
            await asyncio.sleep(0.01)


@pytest.fixture
async def skill_application(config_application: ConfigApplicationCase, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[SkillApplicationCase]:
    case = config_application
    root = case.path.parent
    monkeypatch.setenv("BOXTEAM_HOME", str(root))
    skill = root / "skills/demo/SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("初版正文\n")
    owner = GatewaySkillSourceOwner(gateway_id="gateway_0123456789abcdef0123456789abcdef", skills_root=root / "skills")
    case.app.state.skill_source_owner = owner
    case.app.add_middleware(TraceMiddleware)
    case.app.include_router(skill_source_router)
    async with owner.lifespan(), httpx.AsyncClient(
        transport=httpx.ASGITransport(app=case.app), base_url="http://gateway.test",
        headers={"X-Local-Token": get_gateway_local_token()},
    ) as client:
        yield SkillApplicationCase(case, owner, client, skill)


async def test_local_skill_hot_policy_and_management_refresh_share_accepted_owner(skill_application: SkillApplicationCase) -> None:
    case = skill_application
    old = case.owner.file_registry.observation
    await case.policy("manual")
    registry = case.owner.file_registry
    assert registry.observation.policy.mode == "manual"
    assert old.task is not None and old.task.done() and not old.subscriptions
    assert len(registry._observations) == 1
    before = case.owner.snapshot(workspace_id="workspace")
    case.skill.write_text("仅手动获取的新版\n")
    # 将旧代次的错误和 overflow 都留到切换后，不能让其重新发布。
    await old.events.put(WorkspaceFileChangeBatch(error="旧代次迟到错误"))
    await old.events.put(WorkspaceFileChangeBatch(overflow=True))
    await asyncio.sleep(0.1)
    assert case.owner.snapshot(workspace_id="workspace") == before
    denied = await case.client.post("/api/gateway/skill-sources/refresh", headers={"X-Local-Token": "wrong"})
    assert denied.status_code == 401
    reply = await case.client.post("/api/gateway/skill-sources/refresh")
    assert reply.status_code == 200, reply.text
    assert reply.headers["X-Request-ID"] == reply.json()["request_id"]
    after = case.owner.snapshot(workspace_id="workspace")
    assert after.entries[0].content == "仅手动获取的新版\n"
    assert reply.json()["data"] == {"gateway_id": case.owner.gateway_id, "revision": after.revision, "available": True}
    assert str(case.skill) not in reply.text
    await case.policy("event")
    case.skill.write_text("重新建立监听后的新版\n")
    await _until(lambda: case.owner.snapshot(workspace_id="workspace").entries[0].content == "重新建立监听后的新版\n")
    event = case.config.service.list_events()[-1]
    assert event.result == "applied" and event.activation_scope == "current"
    assert not case.config.service.status().restart_required


@pytest.mark.parametrize("mode", ["poll", "event_poll"])
async def test_local_skill_poll_uses_configured_policy_and_same_reconciler(
    skill_application: SkillApplicationCase, monkeypatch: pytest.MonkeyPatch, mode: str,
) -> None:
    case = skill_application
    # 单元测试只加速时钟周期；产品入口另验证真实的一分钟配置。
    monkeypatch.setattr(SourceUpdatePolicy, "poll_seconds", property(lambda self: 0.08 if self.poll_interval else None))
    await case.policy(mode, "1m")
    current = case.owner.file_registry.observation
    assert current.policy.poll_interval == "1m"
    assert bool(current.subscriptions) == (mode == "event_poll")
    case.skill.write_text("周期获取正文\n")
    await _until(lambda: case.owner.snapshot(workspace_id="workspace").entries[0].content == "周期获取正文\n")
    assert len(case.owner.file_registry._observations) == 1


@pytest.mark.parametrize("failure", ["prepare", "promotion"])
async def test_local_skill_failed_candidate_preserves_original_generation(
    skill_application: SkillApplicationCase, monkeypatch: pytest.MonkeyPatch, failure: str,
) -> None:
    case = skill_application
    await case.policy("manual")
    registry = case.owner.file_registry
    original = registry.observation
    accepted = case.owner.snapshot(workspace_id="workspace")
    baseline = case.config.store.get_active_config_snapshot("gateway")
    if failure == "prepare":
        async def reject_ready(*args: object, **kwargs: object) -> None:
            raise RuntimeError("候选监听未就绪")
        monkeypatch.setattr(registry._watch_service, "wait_until_ready", reject_ready)
    else:
        def reject_promotion(**kwargs: object) -> None:
            raise ConfigConflictError("策略最终 CAS 冲突")
        monkeypatch.setattr(case.config.store, "promote_active_config_snapshot", reject_promotion)
    expected = ConfigConflictError if failure == "promotion" else RuntimeError
    with pytest.raises(expected, match="stage=promotion|stage=runtime_apply") as rejected:
        await case.policy("event")
    cause = rejected.value.__cause__
    if failure == "prepare":
        assert cause is not None and str(cause) == "候选监听未就绪"
    else:
        assert isinstance(cause, ConfigConflictError) and str(cause) == "策略最终 CAS 冲突"
    assert registry.observation is original and original.activated.is_set()
    assert len(registry._observations) == 1 and not original.subscriptions
    assert case.config.store.get_active_config_snapshot("gateway") == baseline
    assert case.owner.snapshot(workspace_id="workspace") == accepted
    case.skill.write_text("失败候选不能恢复自动获取\n")
    await asyncio.sleep(0.1)
    assert case.owner.snapshot(workspace_id="workspace") == accepted


async def test_unsettled_old_observer_is_fenced_and_rollback_restores_it(skill_application: SkillApplicationCase) -> None:
    case = skill_application
    registry = case.owner.file_registry
    original = registry.observation
    result = await GatewayRuntimeConsumerTransaction((case.owner.policy_stage(
        policy=SourceUpdatePolicy(mode="manual"), generation="candidate-proof",
        fencing_token_digest=None, fence_check=None,
    ),)).apply()
    accepted = case.owner.snapshot(workspace_id="workspace")
    assert original.task is not None and not original.task.done()
    await original.events.put(WorkspaceFileChangeBatch(error="未结算旧代次的迟到错误"))
    await asyncio.sleep(0.05)
    assert case.owner.snapshot(workspace_id="workspace") == accepted
    await result.rollback()
    await result.finalize()
    assert registry.observation is original and len(registry._observations) == 1
    case.skill.write_text("回退后旧订阅仍可观察\n")
    await _until(lambda: case.owner.snapshot(workspace_id="workspace").entries[0].content == "回退后旧订阅仍可观察\n")


@dataclass(frozen=True)
class ConfigApplicationCase:
    app: FastAPI
    service: GatewayConfigReloadService
    store: GatewayStateStore
    path: Path
    policy: FederationPolicyStore


@pytest.fixture
def config_application(request: pytest.FixtureRequest) -> Iterator[ConfigApplicationCase]:
    context = TestRunContext.from_test_file(Path(__file__)).prepare()
    root = context.boxteam_home_for_node(request.node.nodeid)
    root.mkdir(parents=True, exist_ok=True)
    path = root / "gateway.jsonc"
    path.write_text(json.dumps({"config_version": 3}), encoding="utf-8")
    with GatewayStateStore(path=root / "gateway.sqlite") as store:
        schema = context.project_root / "configs/gateway_schema.jsonc"
        config = load_gateway_config(config_path=path, local_config_path=root / "gateway_local.jsonc", schema_path=schema, state_store=store)
        app = FastAPI()
        app.state.gateway_config = config
        policy = FederationPolicyStore(initial={})
        app.state.federation_policy_store = policy
        service = GatewayConfigReloadService(
            state_store=store, config=config, config_path=path, local_config_path=root / "gateway_local.jsonc", schema_path=schema,
            on_runtime_config=lambda candidate, previous, token, check: _apply_gateway_runtime_config(
                app, candidate, previous, token, check,
            ),
        )
        service.initialize_active_snapshot()
        yield ConfigApplicationCase(app, service, store, path, policy)


@pytest.mark.asyncio
@pytest.mark.parametrize("conflict", [False, True])
async def test_reload_persists_real_proofs_and_compensates_the_frozen_consumers(
    config_application: ConfigApplicationCase, monkeypatch: pytest.MonkeyPatch, conflict: bool,
) -> None:
    case = config_application
    initial = case.service.config
    baseline = case.store.get_active_config_snapshot("gateway")
    replacement = FederationPolicyStore(initial={"default_effect": "deny"})
    case.path.write_text(json.dumps({
        "config_version": 3, "permissions": {"federation": {"default_effect": "deny"}},
    }), encoding="utf-8")

    def reject_promotion(**kwargs):
        assert case.policy.snapshot.default_effect == "deny"
        case.app.state.federation_policy_store = replacement
        raise ConfigConflictError("最终 CAS 冲突")

    if conflict:
        monkeypatch.setattr(case.store, "promote_active_config_snapshot", reject_promotion)
        with pytest.raises(ConfigConflictError, match="stage=promotion"):
            await case.service.reload()
        assert case.app.state.gateway_config is initial
        assert case.service.config is initial
        assert case.policy.snapshot.default_effect == "allow"
        assert replacement.snapshot.default_effect == "deny"
        assert case.store.get_active_config_snapshot("gateway") == baseline
    else:
        await case.service.reload()
        assert case.policy.snapshot.default_effect == "deny"
        assert case.app.state.gateway_config is case.service.config
        assert case.service.config.revision != initial.revision

    event = case.service.list_events()[-1]
    assert event.apply_id is not None
    journal = case.store.get_config_apply_journal(apply_id=event.apply_id)
    assert journal is not None and journal.state == ("compensated" if conflict else "committed")
    applied = next(effect for effect in journal.side_effects if effect["action"] == "apply")
    proofs = applied["consumer_proofs"]
    assert isinstance(proofs, list)
    assert {proof["consumer_id"] for proof in proofs} == {"federation-policy", "gateway-config-snapshot"}
    assert len({proof["generation"] for proof in proofs}) == 1
    assert all(proof["state"] == "healthy" and len(proof["fencing_token_digest"]) == 64 for proof in proofs)


@dataclass
class LoggingApplicationCase:
    config: ConfigApplicationCase
    registry: GatewayWorkspaceRegistry
    states: dict[int, RuntimeLoggingStatus]
    calls: list[tuple[int, str]]
    failure: str | None = None


@pytest.fixture
def logging_application(
    config_application: ConfigApplicationCase, monkeypatch: pytest.MonkeyPatch, managed_backend_factory,
) -> Iterator[LoggingApplicationCase]:
    config = config_application
    monkeypatch.setenv("BOXTEAM_HOME", str(config.path.parent))
    monkeypatch.setenv("BOXTEAM_LAUNCHER_CONTROL", json.dumps({"address": "/fixture/socket", "pid": 20}))
    for name in ("", "uvicorn", "uvicorn.error", "uvicorn.access"):
        logger = logging.getLogger(name)
        monkeypatch.setattr(logger, "handlers", [])
        monkeypatch.setattr(logger, "level", logger.level)
        monkeypatch.setattr(logger, "propagate", logger.propagate)
    configure_application_logging(service="gateway", stream=io.StringIO(), revision=config.service.config.revision)
    registry = GatewayWorkspaceRegistry(
        storage_path=config.path.parent / "workspaces.json",
        runtime_logging_config=lambda: {"revision": config.app.state.gateway_config.revision},
    )
    targets = tuple(WorkspaceTarget(
        workspace_id=f"logging-{index}", name=f"workspace-{index}",
        root_path=str(config.path.parent / f"workspace-{index}"), backend_url="http://127.0.0.1:42000",
        backend_workspace_id=f"00000000-0000-4000-8000-00000000000{index}",
        connection_kind="local", managed=True,
    ) for index in (1, 2))
    for target in targets:
        registry.upsert(target)
    graph = managed_backend_factory(
        targets, backend_url="http://127.0.0.1:42000", process=ProcessProbe(pid=21),
        auxiliary_processes={target.workspace_id: {
            "terminal_manager": ProcessProbe(pid=22 + index * 2),
            "browser_manager": ProcessProbe(pid=23 + index * 2),
        } for index, target in enumerate(targets)},
    )
    graph.publish(registry)
    # 即使地址在本机，未受管的外部 Backend 也不参加日志控制。
    registry.upsert(WorkspaceTarget(
        "external", "external", str(config.path.parent / "external"), "http://127.0.0.1:49999", "local",
        backend_workspace_id="00000000-0000-4000-8000-000000000003",
    ))
    roles: tuple[tuple[int, RuntimeLoggingService], ...] = ((20, "launcher"), (21, "backend"), (22, "terminal"), (23, "browser"), (24, "terminal"), (25, "browser"))
    states = {pid: RuntimeLoggingStatus(
        service=role, pid=pid, runtime_instance_id=f"instance-{pid}",
        applied_revision=config.service.config.revision, level="info", pretty=True, log_sink=None,
    ) for pid, role in roles}
    case = LoggingApplicationCase(config, registry, states, [])
    config.app.state.registry = registry
    config.app.state.gateway_state = config.store

    async def status(_self, *, pid: int, **_kwargs) -> RuntimeLoggingStatus:
        return states[pid]

    async def apply(_self, *, pid: int, request: RuntimeLoggingApply, **_kwargs) -> RuntimeLoggingStatus:
        assert registry.backend_topology_lock.locked()
        case.calls.append((pid, request.revision))
        current = states[pid]
        if case.failure == "rollback" and pid == 21 and request.revision == config.service.config.revision:
            raise RuntimeError("真实补偿拒绝")
        assert current.runtime_instance_id == request.runtime_instance_id
        assert current.applied_revision in {request.expected_revision, request.revision}
        if pid == 23 and case.failure == "reject" and request.revision != config.service.config.revision:
            raise RuntimeError("应用请求被拒绝")
        states[pid] = current.model_copy(update={"applied_revision": request.revision, "level": request.level, "pretty": request.pretty})
        if pid == 23 and case.failure in {"replace", "foreign"} and request.revision != config.service.config.revision:
            states[pid] = states[pid].model_copy(update={"runtime_instance_id": "replacement"} if case.failure == "replace" else {"applied_revision": "external-owner"})
        if pid == 23 and case.failure in {"timeout", "rollback"} and request.revision != config.service.config.revision:
            raise TimeoutError("已生效但响应丢失")
        return states[pid]

    monkeypatch.setattr(RuntimeLoggingProcessControl, "status", status)
    monkeypatch.setattr(RuntimeLoggingProcessControl, "launcher_status", status)
    monkeypatch.setattr(RuntimeLoggingProcessControl, "apply", apply)
    monkeypatch.setattr(RuntimeLoggingProcessControl, "launcher_apply", apply)
    try:
        yield case
    finally:
        registry.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("failed", [False, True])
async def test_logging_diagnostics_uses_shared_owners_and_keeps_paths_in_raw_logs(
    logging_application: LoggingApplicationCase, monkeypatch: pytest.MonkeyPatch, failed: bool,
) -> None:
    case = logging_application
    path = case.config.path.parent / "private-backend.log"
    case.states[21] = case.states[21].model_copy(update={"log_sink": RuntimeLoggingSink(
        path=str(path), owner_id="test-log-owner", role="backend", source="gateway", device="1", inode="2",
    )})
    if failed:
        original = RuntimeLoggingProcessControl.status

        async def reject_backend(self, *, pid: int, **kwargs) -> RuntimeLoggingStatus:
            if pid == 21:
                raise OSError(f"cannot read private sink {path}")
            return await original(self, pid=pid, **kwargs)

        monkeypatch.setattr(RuntimeLoggingProcessControl, "status", reject_backend)
    candidates = [_LogCandidate("backend-log", "workspace", "workspace_api", "Backend", path)]
    processes = await _logging_process_diagnostics(case.registry, None, candidates)
    assert len(processes) == 7
    backend, = [process for process in processes if process.service == "backend"]
    assert backend.workspace_ids == ["logging-1", "logging-2"]
    public = json.dumps([process.model_dump(mode="json") for process in processes])
    assert str(case.config.path.parent) not in public
    if failed:
        assert backend.state is None and backend.output_mode == "unknown"
        assert backend.error is not None and "OSError" in backend.error
    else:
        assert backend.error is None and backend.output_mode == "file"
        assert backend.state is not None and backend.state.log_sink is not None
        assert backend.state.log_sink.log_id == "backend-log"
        assert backend.state.log_sink.owner_id == "test-log-owner"
    assert all(process.output_mode == "stdout-only" for process in processes if process.service != "backend")
    assert not case.calls, "只读诊断不能应用或补偿设置"
    scoped = await _logging_process_diagnostics(case.registry, "logging-2", candidates)
    assert len(scoped) == 5
    assert all(not process.workspace_ids or "logging-2" in process.workspace_ids for process in scoped)
    assert not case.registry.backend_topology_lock.locked()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [None, "reject", "timeout", "rollback", "cas", "replace", "foreign", "diagnostic"])
async def test_logging_reload_confirms_shared_processes_and_settles_actual_failures(
    logging_application: LoggingApplicationCase, monkeypatch: pytest.MonkeyPatch, failure: str | None,
) -> None:
    case = logging_application
    config = case.config
    before = dict(case.states)
    previous = config.service.config
    active = config.store.get_active_config_snapshot("gateway")
    case.failure = failure
    if failure == "diagnostic":
        case.failure = "timeout"

        def fail_diagnostic(*_args, **_kwargs):
            raise OSError("产品诊断 sink 无法写入")

        monkeypatch.setattr("app.gateway.config.reload_lifecycle.logger.error", fail_diagnostic)
    config.path.write_text(json.dumps({"config_version": 3, "logging": {"runtime": {"level": "debug", "pretty": False}}}))

    def reject_promotion(**_kwargs):
        assert case.registry.backend_topology_lock.locked()
        assert all(state.level == "debug" for state in case.states.values())
        assert runtime_logging_state().level == "debug"
        raise ConfigConflictError("最终 CAS 冲突")

    if failure == "cas":
        monkeypatch.setattr(config.store, "promote_active_config_snapshot", reject_promotion)
    if failure is None:
        await config.service.reload()
        assert config.service.config.revision != previous.revision
        assert all(state.applied_revision == config.service.config.revision and state.level == "debug" and not state.pretty for state in case.states.values())
        assert len(case.calls) == 6
        assert len([pid for pid, _ in case.calls if pid == 21]) == 1
    else:
        with pytest.raises(RuntimeError, match="stage=") as raised:
            await config.service.reload()
        if failure == "diagnostic":
            assert "diagnostic_sink_error=OSError" in str(raised.value)
            cause = raised.value.__cause__
            assert cause is not None and "诊断日志输出失败" in str(cause.__notes__)
        assert config.service.config is previous
        assert config.app.state.gateway_config is previous
        assert config.store.get_active_config_snapshot("gateway") == active
        if failure in {"rollback", "replace", "foreign"}:
            retained_pid = 21 if failure == "rollback" else 23
            assert case.states[retained_pid].level == "debug"
            assert {pid: state for pid, state in case.states.items() if pid != retained_pid} == {pid: state for pid, state in before.items() if pid != retained_pid}
            if failure == "replace":
                assert case.states[23].runtime_instance_id == "replacement"
            elif failure == "foreign":
                assert case.states[23].applied_revision == "external-owner"
        else:
            assert case.states == before
        assert runtime_logging_state().applied_revision == previous.revision
    assert not case.registry.backend_topology_lock.locked()
    event = config.service.list_events()[-1]
    assert event.apply_id is not None
    journal = config.store.get_config_apply_journal(apply_id=event.apply_id)
    assert journal is not None
    assert journal.state == ("committed" if failure is None else "recovery_required" if failure in {"rollback", "replace", "foreign"} else "compensated")
    if failure in {"timeout", "rollback", "diagnostic"}:
        unknown = [item for item in journal.side_effects if item["action"] == "outcome_unknown"]
        assert len(unknown) == 1 and unknown[0]["current"] is None
        previous_state = unknown[0]["before"]
        assert isinstance(previous_state, dict) and previous_state["pid"] == 23
    startup = case.registry.runtime_logging_startup()
    assert startup is not None and startup["revision"] == config.app.state.gateway_config.revision


@pytest.mark.asyncio
async def test_mixed_restart_candidate_does_not_apply_logging_early(logging_application: LoggingApplicationCase) -> None:
    case = logging_application
    before = dict(case.states)
    case.config.path.write_text(json.dumps({
        "config_version": 3, "logging": {"runtime": {"level": "warning"}},
        "runtime": {"gateway": {"process": {"health": {"poll_interval_seconds": 1.5}}}},
    }))
    await case.config.service.reload()
    assert case.config.service.status().state == "pending_restart"
    assert case.states == before and case.calls == []
    assert not case.registry.backend_topology_lock.locked()
