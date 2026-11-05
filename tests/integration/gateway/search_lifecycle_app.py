"""显式测试组合：在原 canonical 准入 worker 内观察关闭，不增加产品控制 API。"""

from __future__ import annotations

import asyncio
import json
import os
import threading
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Literal

from fastapi import FastAPI
from pydantic import BaseModel, ConfigDict

import app.container as container_module
from app.schemas.internal_v2.search import (
    SearchProviderCapabilityDTO,
    WorkspaceSearchRequest,
    WorkspaceSearchResultDTO,
)
from app.schemas.internal_v2.turn_search import (
    TurnMessageSearchRequest,
    TurnMessageSearchResultsDTO,
)
from app.services.business.search.service import (
    SearchContractError,
    SearchQuery,
    SearchRegistration,
    WorkspaceSearchService,
)
from app.services.business.session_service import SessionService
from app.services.business.session_turn_history.service import SessionTurnHistoryService
from app.services.infrastructure.rollout_history.message_search import (
    MessageSearchBudget,
)
from tests.support.e2e_control.backend_app import app

__all__ = ["app"]

_canonical_session: ContextVar[str | None] = ContextVar("canonical_search_session", default=None)


class Command(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["arm", "release", "close_registration", "close_service", "register_v2", "snapshot"]
    label: str
    session_id: str = ""


class ReadGate:
    def __init__(self, session_id: str, label: str) -> None:
        self.session_id = session_id
        self.label = label
        self.release = threading.Event()
        self.claimed = False


class LifecycleControl:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.audit = root / "events.jsonl"
        self._write_lock = threading.Lock()
        self._sequence = 0
        self.gate: ReadGate | None = None
        self.service: ObservedSearchService | None = None
        self.closing: dict[str, asyncio.Task[None]] = {}

    def record(self, event: str, **detail: object) -> None:
        with self._write_lock:
            self._sequence += 1
            with self.audit.open("a", encoding="utf-8") as output:
                output.write(json.dumps({"sequence": self._sequence, "event": event, "pid": os.getpid(),
                                         "thread": threading.get_ident(), **detail}, ensure_ascii=False) + "\n")

    def worker_admitted(self, session_id: str) -> None:
        gate = self.gate
        if gate is None or gate.session_id != session_id or gate.claimed or _canonical_session.get() != session_id:
            return
        # super 已验证原 catalog；原 run_sync_shielded 仍拥有本 worker，原 shared guard 仍持有。
        gate.claimed = True
        self.record("read_worker_enter", label=gate.label, session_id=session_id, owner="SessionTurnHistoryService.search_messages")
        if not gate.release.wait(timeout=45):
            raise TimeoutError(f"测试未释放原 Session 准入 worker: {gate.label}/{session_id}")
        self.record("read_worker_exit", label=gate.label, session_id=session_id)

    def snapshot(self) -> dict[str, object]:
        service = self.service
        assert service is not None
        return {
            "active": [item.provider_id for item in service.capabilities().providers],
            "registrations": {key: {"active": item.active, "tasks": len(item.tasks)}
                              for key, item in service.registrations.items()},
            "closing": {key: task.done() for key, task in self.closing.items()},
        }

    async def close(self, label: str, close: Callable[[], Awaitable[None]]) -> None:
        await close()
        self.record("close_complete", label=label)

    async def dispatch(self, command: Command) -> dict[str, object]:
        service = self.service
        assert service is not None
        match command.action:
            case "arm":
                if self.gate is not None and not self.gate.release.is_set():
                    raise RuntimeError("前一个 canonical 准入 worker 尚未释放")
                self.gate = ReadGate(command.session_id, command.label)
                self.record("read_armed", label=command.label, session_id=command.session_id)
            case "release":
                gate = self.gate
                assert gate is not None and gate.label == command.label and gate.claimed
                self.record("read_release_requested", label=command.label)
                gate.release.set()
            case "close_registration":
                registration = service.registrations["canonical-message"]
                registration.close_admission()
                self.record("admission_closed", label=command.label, provider_id=registration.capability.provider_id)
                self.closing[command.label] = asyncio.create_task(self.close(command.label, registration.aclose))
            case "close_service":
                self.closing[command.label] = asyncio.create_task(self.close(command.label, service.aclose))
                # aclose 的原同步前段关闭全部准入后，才向测试回执当前真实注册状态。
                await asyncio.sleep(0)
                self.record("admission_closed", label=command.label, provider_id="workspace_search_service")
            case "register_v2":
                original = service.registrations["canonical-message"]

                async def query(request: WorkspaceSearchRequest, gateway_id: str | None) -> WorkspaceSearchResultDTO:
                    # 测试版本仅改变注册信封；正文仍由原 canonical owner 查询，不生成替代命中。
                    page = await service.queries["canonical-message"](request, gateway_id)
                    return page.model_copy(update={"provider_id": "canonical-message-v2", "contract_version": 2})

                service.register(original.capability.model_copy(update={
                    "provider_id": "canonical-message-v2", "contract_version": 2,
                }), query)
                self.record("v2_registered", label=command.label)
            case "snapshot":
                pass
        return self.snapshot()

    async def run(self) -> None:
        sequence = 1
        while True:
            path = self.root / f"command-{sequence}.json"
            if not path.exists():
                await asyncio.sleep(0.02)
                continue
            command = Command.model_validate_json(path.read_bytes())
            try:
                state = await self.dispatch(command)
                reply: dict[str, object] = {"state": state}
            except ValueError as error:
                if command.action != "register_v2":
                    raise
                self.record("registration_rejected", label=command.label, error_type=type(error).__name__, message=str(error))
                reply = {"error_type": type(error).__name__, "message": str(error),
                         "code": error.code if isinstance(error, SearchContractError) else None}
            target = self.root / f"reply-{sequence}.json"
            temporary = target.with_suffix(".tmp")
            temporary.write_text(json.dumps(reply, ensure_ascii=False) + "\n", encoding="utf-8")
            temporary.replace(target)
            sequence += 1


_control = LifecycleControl(Path(os.environ["BOXTEAM_SEARCH_LIFECYCLE_CONTROL"]))


class ObservedSessionService(SessionService):
    def assert_session_active(self, session_id: str) -> None:
        super().assert_session_active(session_id)
        _control.worker_admitted(session_id)


class ObservedSessionTurnHistoryService(SessionTurnHistoryService):
    async def search_messages(
        self, session_id: str, thread_id: str, request: TurnMessageSearchRequest,
        *, budget: MessageSearchBudget | None = None,
    ) -> TurnMessageSearchResultsDTO:
        # ContextVar 沿原 asyncio.to_thread 传播，只阻塞 canonical 入口本身的准入 worker。
        token = _canonical_session.set(session_id)
        try:
            return await super().search_messages(session_id, thread_id, request, budget=budget)
        finally:
            _canonical_session.reset(token)


class ObservedSearchService(WorkspaceSearchService):
    def __init__(self, workspace_id: str) -> None:
        super().__init__(workspace_id)
        self.registrations: dict[str, SearchRegistration] = {}
        self.queries: dict[str, SearchQuery] = {}

    def register(
        self, capability: SearchProviderCapabilityDTO, query: SearchQuery,
        *, release: Callable[[], Awaitable[None]] | None = None,
    ) -> SearchRegistration:
        async def observed_query(request: WorkspaceSearchRequest, gateway_id: str | None) -> WorkspaceSearchResultDTO:
            _control.record("query_enter", provider_id=capability.provider_id, contract_version=request.contract_version)
            try:
                result = await query(request, gateway_id)
            except BaseException as error:  # 观察原取消及异常，原实例立即重抛。
                _control.record("query_exit", provider_id=capability.provider_id, error_type=type(error).__name__)
                raise
            _control.record("query_exit", provider_id=capability.provider_id, error_type=None)
            return result

        async def observed_release() -> None:
            if release is not None:
                await release()
            _control.record("provider_released", provider_id=capability.provider_id)

        registration = super().register(capability, observed_query, release=observed_release)
        self.registrations[capability.provider_id] = registration
        self.queries[capability.provider_id] = query
        return registration


# 仅此显式测试入口选择继承观察器；原容器、领域查询、HTTP 和关闭实现全部保留。
container_module.SessionService = ObservedSessionService
container_module.SessionTurnHistoryService = ObservedSessionTurnHistoryService
container_module.WorkspaceSearchService = ObservedSearchService
_original_lifespan = app.router.lifespan_context


@asynccontextmanager
async def observed_lifespan(application: FastAPI) -> AsyncIterator[None]:
    async with _original_lifespan(application):
        registry = application.state.workspace_container_registry
        workspace_id, = registry.workspace_ids
        service = registry.get(workspace_id).workspace_search_service
        assert isinstance(service, ObservedSearchService)
        _control.service = service
        _control.record("ready", workspace_id=workspace_id, **_control.snapshot())
        controller = asyncio.create_task(_control.run())
        try:
            yield
        finally:
            if _control.gate is not None:
                _control.gate.release.set()
            if controller.done():
                controller.result()
            else:
                controller.cancel()
                await asyncio.gather(controller, return_exceptions=True)
            await asyncio.gather(*_control.closing.values())


app.router.lifespan_context = observed_lifespan
