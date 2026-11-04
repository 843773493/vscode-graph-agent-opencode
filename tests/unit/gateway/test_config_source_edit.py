"""Gateway 来源编辑走真实 loader、状态库、reload 与 ASGI 边界。"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from app.core.trace_middleware import TraceMiddleware
from app.gateway.auth import GatewayAuthContext, verify_gateway_access
from app.gateway.config import (
    GatewayConfig,
    GatewayConfigReloadService,
    load_gateway_config,
)
from app.gateway.control.gateway_state import GatewayStateStore
from app.gateway.federation.policy import Capability, FederationPolicyStore
from app.gateway.federation.ports import principal_ref_for_gateway
from app.gateway.routes.health_config import router
from app.gateway.runtime.consumer_protocol import GatewayRuntimeConsumerApplyResult
from app.gateway.runtime_proof import _apply_gateway_runtime_config
from app.schemas.gateway.config import GatewayConfigSourceEditRequest
from app.services.infrastructure.config.state import ConfigConflictError

SOURCE = "gateway_mutable_override"
LOCAL = "gateway_local_mutable_override"
GATEWAY = "gateway_" + "b" * 32
PEER = "gateway_" + "a" * 32


@dataclass
class SourceOwner:
    path: Path
    local: Path
    store: GatewayStateStore
    service: GatewayConfigReloadService
    app: FastAPI
    policy: FederationPolicyStore
    calls: list[str] = field(default_factory=list)
    entered: asyncio.Event = field(default_factory=asyncio.Event)
    release: asyncio.Event = field(default_factory=asyncio.Event)
    blocked: bool = False

    def request(self, text: str, *, source: str = SOURCE, key: str = "source-edit") -> GatewayConfigSourceEditRequest:
        document = self.service.get_source_document(source_key=source)
        return GatewayConfigSourceEditRequest(
            gateway_id=document.gateway_id, source_key=source, jsonc=text,
            base_file_digest=document.file_digest, base_layer_revision=document.layer_revision,
            base_layer_digest=document.layer_digest, expected_active_revision=document.active_revision,
            expected_active_digest=document.active_digest, idempotency_key=key,
        )

    def snapshot(self) -> tuple[str, dict[str, bytes]]:
        with sqlite3.connect(self.store.path) as connection:
            database = "\n".join(connection.iterdump())
        return database, {p.name: p.read_bytes() for p in self.path.parent.iterdir() if p.is_file()}

    def allow(self, *capabilities: Capability) -> None:
        self.policy.publish({"rules": [
            {"capability": value, "effect": "allow", "principal_ref": principal_ref_for_gateway(PEER)}
            for value in capabilities
        ]})

    def rebuild(self) -> GatewayConfigReloadService:
        config = load_gateway_config(
            config_path=self.path, local_config_path=self.local, state_store=self.store,
            schema_path=self.service.config.schema_path, startup=True, gateway_id=GATEWAY,
        )
        self.service = GatewayConfigReloadService(
            state_store=self.store, config=config, config_path=self.path,
            local_config_path=self.local, schema_path=config.schema_path,
            gateway_id=GATEWAY, on_runtime_config=self.apply,
        )
        self.app.state.gateway_config_reload = self.service
        return self.service

    async def apply(
        self, candidate: GatewayConfig, _previous: GatewayConfig, _fence: str,
        assert_claim: Callable[[], None],
    ) -> GatewayRuntimeConsumerApplyResult:
        assert_claim()
        self.calls.append(candidate.revision)
        self.entered.set()
        if self.blocked:
            await self.release.wait()
        assert_claim()
        return GatewayRuntimeConsumerApplyResult(stages=(), proofs=())


@pytest.fixture
def source_owner(tmp_path: Path) -> Iterator[SourceOwner]:
    config_root = tmp_path / "config"
    config_root.mkdir()
    path, local = config_root / "gateway.jsonc", config_root / "gateway_local.jsonc"
    path.write_text('{"config_version":4,"ui":{"theme":{"default_theme_id":"warm"}}}\n')
    schema = Path.cwd() / "configs/gateway_schema.jsonc"
    with GatewayStateStore(path=tmp_path / "gateway.sqlite") as store:
        config = load_gateway_config(config_path=path, local_config_path=local, schema_path=schema, state_store=store)
        service = GatewayConfigReloadService(
            state_store=store, config=config, config_path=path, local_config_path=local,
            schema_path=schema, gateway_id=GATEWAY,
        )
        service.initialize_active_snapshot()
        app = FastAPI()
        app.add_middleware(TraceMiddleware)
        app.include_router(router)
        policy = FederationPolicyStore()
        app.state.gateway_state = store
        app.state.gateway_config_reload = service
        app.state.federation_policy_store = policy
        app.dependency_overrides[verify_gateway_access] = lambda: GatewayAuthContext(kind="federation", peer_gateway_id=PEER)
        case = SourceOwner(path, local, store, service, app, policy)
        service._on_runtime_config = case.apply
        yield case


def test_get_and_preflight_are_pure_even_for_already_imported_digest(source_owner: SourceOwner) -> None:
    case = source_owner
    request = case.request('{ // 保留注释\n "config_version":4,"ui":{"theme":{"default_theme_id":"blue"}}}\n')
    before = case.snapshot()
    preview = case.service.preflight_source_edit(source_key=SOURCE, request=request)
    assert preview.changed_paths == ["/ui/theme/default_theme_id"]
    assert preview.document.effective["ui"] != preview.candidate["ui"]
    assert case.service.get_source_document(source_key=SOURCE).jsonc == case.path.read_text()
    assert case.snapshot() == before
    assert not case.calls


@pytest.mark.parametrize(("field_name", "value"), [
    ("gateway_id", "wrong-gateway"), ("source_key", LOCAL),
    ("base_file_digest", "changed-file"), ("base_layer_revision", 999),
    ("base_layer_digest", "changed-layer"), ("expected_active_revision", 999),
    ("expected_active_digest", "changed-active"),
])
async def test_each_target_and_cas_conflict_writes_nothing(
    source_owner: SourceOwner, field_name: str, value: object,
) -> None:
    case = source_owner
    request = case.request('{"ui":{"theme":{"default_theme_id":"blue"}}}')
    request = GatewayConfigSourceEditRequest.model_validate({**request.model_dump(), field_name: value})
    before = case.snapshot()
    with pytest.raises(ConfigConflictError):
        case.service.preflight_source_edit(source_key=SOURCE, request=request)
    with pytest.raises(ConfigConflictError):
        await case.service.save_source_edit(source_key=SOURCE, request=request)
    assert case.snapshot() == before
    assert not case.calls


async def test_inline_is_readonly_and_unknown_source_is_rejected(source_owner: SourceOwner) -> None:
    case = source_owner
    inline = case.service.get_source_document(source_key="inline:0")
    assert not inline.writable and inline.layer == "inline"
    request = case.request("{}", source="inline:0")
    before = case.snapshot()
    with pytest.raises(ValueError, match="只读"):
        await case.service.save_source_edit(source_key="inline:0", request=request)
    with pytest.raises(ValueError, match="未知"):
        case.service.get_source_document(source_key="sqlite:path")
    assert case.snapshot() == before


async def test_user_and_local_share_writer_and_removing_local_reveals_user(source_owner: SourceOwner) -> None:
    case = source_owner
    request = case.request('{"ui":{"theme":{"default_theme_id":"green"}}}', source=LOCAL, key="local")
    local = await case.service.save_source_edit(source_key=LOCAL, request=request)
    assert local.edit is not None and local.edit.state == "active"
    assert local.layer == "user_local" and local.file_presence == "present"
    before_active, before_calls = local.active_revision, len(case.calls)
    request = case.request('{ // 被 local 遮蔽\n"ui":{"theme":{"default_theme_id":"blue"}}}\n', key="shadowed")
    saved = await case.service.save_source_edit(source_key=SOURCE, request=request)
    assert saved.edit is not None and saved.edit.state == "active" and saved.edit.layer_imported
    assert saved.active_revision == before_active and len(case.calls) == before_calls
    origin = next(item for item in saved.origins if item.path == "/ui/theme/default_theme_id")
    assert origin.source_key == LOCAL and SOURCE in origin.shadowed_sources
    request = case.request("{}\n", source=LOCAL, key="remove-local")
    cleared = await case.service.save_source_edit(source_key=LOCAL, request=request)
    assert cleared.effective["ui"] == {"theme": {"default_theme_id": "blue", "custom_themes": []}}
    assert len(case.calls) == before_calls + 1
    assert len(case.service.list_events()) == 2


async def test_comment_only_save_preserves_bytes_and_does_not_promote(source_owner: SourceOwner) -> None:
    case = source_owner
    text = '{ // 只修改注释、空白与尾逗号\n "config_version":4,"ui":{"theme":{"default_theme_id":"warm"}},\n}\n'
    request = case.request(text)
    active = case.store.get_active_config_snapshot("gateway")
    saved = await case.service.save_source_edit(source_key=SOURCE, request=request)
    assert saved.edit is not None and saved.edit.state == "active"
    assert saved.edit.source_saved and saved.edit.layer_imported
    assert case.path.read_text() == text
    assert case.store.get_active_config_snapshot("gateway") == active
    assert not case.calls and not case.service.list_events()
    assert await case.service.save_source_edit(source_key=SOURCE, request=request) == saved


def _connection(host: str = "known.test", *, explicit: bool = True) -> dict[str, object]:
    value: dict[str, object] = {"kind": "remote_gateway", "host": host, "username": "tester", "private_key_path": "keys/id_test"}
    if explicit:
        value["connection_id"] = "connection_known"
    return value


async def test_v4_existing_missing_id_reads_and_saves_from_original_identity_owner(source_owner: SourceOwner) -> None:
    case = source_owner
    case.path.write_text(json.dumps({"config_version": 4, "workspaces": [_connection()]}))
    # 仅让原 loader 建立原 identity owner，不借 source writer 派生新映射。
    load_gateway_config(config_path=case.path, local_config_path=case.local, schema_path=case.service.config.schema_path, state_store=case.store)
    case.path.write_text(json.dumps({"config_version": 4, "workspaces": [_connection(explicit=False)]}))
    case.rebuild()
    before = case.snapshot()
    document = case.service.get_source_document(source_key=SOURCE)
    assert document.source_effective["workspaces"] == [_connection()]
    request = case.request(case.path.read_text() + "\n// 原正文不插入 ID\n")
    preview = case.service.preflight_source_edit(source_key=SOURCE, request=request)
    assert preview.candidate["workspaces"] == [_connection()]
    assert case.snapshot() == before
    # 现 active 仍是空连接；本次提交由原 pending_restart 持有，文件字节不得被迁移改写。
    saved = await case.service.save_source_edit(source_key=SOURCE, request=request)
    assert saved.edit is not None and saved.edit.state == "pending_restart"
    assert case.path.read_text() == request.jsonc
    assert not case.calls


@pytest.mark.parametrize("same_position", [False, True])
async def test_new_missing_id_never_borrows_previous_position(source_owner: SourceOwner, same_position: bool) -> None:
    case = source_owner
    if same_position:
        case.path.write_text(json.dumps({"config_version": 4, "workspaces": [_connection()]}))
        load_gateway_config(config_path=case.path, local_config_path=case.local, schema_path=case.service.config.schema_path, state_store=case.store)
    text = json.dumps({"config_version": 4, "workspaces": [_connection("different.test", explicit=False)]})
    request = case.request(text)
    before = case.snapshot()
    with pytest.raises(ConfigConflictError, match=r"workspaces\[0\].connection_id"):
        case.service.preflight_source_edit(source_key=SOURCE, request=request)
    with pytest.raises(ConfigConflictError, match=r"workspaces\[0\].connection_id"):
        await case.service.save_source_edit(source_key=SOURCE, request=request)
    assert case.snapshot() == before
    with_id = _connection("different.test")
    with_id["connection_id"] = "connection_new"
    request = case.request(json.dumps({"config_version": 4, "workspaces": [with_id]}))
    saved = await case.service.save_source_edit(source_key=SOURCE, request=request)
    assert saved.edit is not None and saved.edit.state == "pending_restart"


async def test_only_v1_missing_id_that_would_rewrite_file_is_rejected(source_owner: SourceOwner) -> None:
    case = source_owner
    case.path.write_text(json.dumps({"config_version": 4, "workspaces": [_connection()]}))
    load_gateway_config(config_path=case.path, local_config_path=case.local, schema_path=case.service.config.schema_path, state_store=case.store)
    text = json.dumps({"config_version": 1, "workspaces": [_connection(explicit=False)]})
    case.path.write_text(text)
    before = case.snapshot()
    document = case.service.get_source_document(source_key=SOURCE)
    assert document.source_effective["workspaces"] == [_connection()]
    request = case.request(text + "\n// v1\n")
    with pytest.raises(ConfigConflictError, match="不兼作版本迁移入口"):
        case.service.preflight_source_edit(source_key=SOURCE, request=request)
    with pytest.raises(ConfigConflictError, match="不兼作版本迁移入口"):
        await case.service.save_source_edit(source_key=SOURCE, request=request)
    assert case.snapshot() == before
    request = case.request(json.dumps({"config_version": 1, "workspaces": [_connection()]}))
    saved = await case.service.save_source_edit(source_key=SOURCE, request=request)
    assert saved.edit is not None and saved.edit.state == "pending_restart"
    assert case.path.read_text() == request.jsonc


async def test_same_key_concurrency_and_watcher_apply_once(source_owner: SourceOwner) -> None:
    case = source_owner
    request = case.request('{"ui":{"theme":{"default_theme_id":"blue"}}}')
    case.blocked = True
    first = asyncio.create_task(case.service.save_source_edit(source_key=SOURCE, request=request))
    await asyncio.wait_for(case.entered.wait(), 2)
    try:
        replay = await asyncio.wait_for(case.service.save_source_edit(source_key=SOURCE, request=request), 2)
        assert replay.edit is not None and replay.edit.state == "applying"
        watcher = asyncio.create_task(case.service.reload())
        await asyncio.sleep(0)
        assert not watcher.done()
        changed = GatewayConfigSourceEditRequest.model_validate({**request.model_dump(), "jsonc": "{}"})
        with pytest.raises(ConfigConflictError, match="其它请求"):
            await case.service.save_source_edit(source_key=SOURCE, request=changed)
    finally:
        case.release.set()
    saved = await asyncio.wait_for(first, 2)
    await asyncio.wait_for(watcher, 2)
    assert saved.edit is not None and saved.edit.candidate_id == replay.edit.candidate_id
    assert len(case.calls) == 1 and len(case.service.list_events()) == 1
    pending = case.store.get_pending_config_candidate(config_domain="gateway", idempotency_key=request.idempotency_key)
    assert pending is not None and pending.source_edit is not None and pending.source_edit["stage"] == "imported"
    assert "jsonc" not in pending.source_edit


@pytest.mark.parametrize("failure", ["before_replace", "after_replace"])
@pytest.mark.parametrize("change", ["hot", "unchanged", "restart"])
async def test_rebuilt_owner_real_watcher_start_recovers_original_source_candidate(
    source_owner: SourceOwner, monkeypatch: pytest.MonkeyPatch, failure: str, change: str,
) -> None:
    from app.gateway.config import source_edit

    case = source_owner
    text = {"hot": '{"ui":{"theme":{"default_theme_id":"blue"}}}',
            "unchanged": case.path.read_text() + "// 仅注释\n",
            "restart": '{"runtime":{"gateway":{"process":{"health":{"request_timeout_seconds":3}}}}}'}[change]
    request = case.request(text)
    original = source_edit.atomic_write_bytes

    def fail(path: Path, data: bytes) -> None:
        if failure == "after_replace":
            original(path, data)
        raise RuntimeError("模拟 replace 进程中断")

    monkeypatch.setattr(source_edit, "atomic_write_bytes", fail)
    with pytest.raises(RuntimeError, match="进程中断"):
        await case.service.save_source_edit(source_key=SOURCE, request=request)
    accepted = case.store.get_pending_config_candidate(config_domain="gateway", idempotency_key=request.idempotency_key)
    assert accepted is not None and accepted.state == "candidate_validated"
    monkeypatch.setattr(source_edit, "atomic_write_bytes", original)
    rebuilt = case.rebuild()
    await rebuilt.start()
    try:
        if failure == "before_replace":
            observed = rebuilt.get_source_document(source_key=SOURCE, idempotency_key=request.idempotency_key)
            assert observed.edit is not None and not observed.edit.source_saved
            saved = await rebuilt.save_source_edit(source_key=SOURCE, request=request)
        else:
            saved = rebuilt.get_source_document(source_key=SOURCE, idempotency_key=request.idempotency_key)
        assert saved.edit is not None and saved.edit.candidate_id == accepted.candidate_id
        assert saved.edit.source_saved and saved.edit.layer_imported
        assert saved.edit.state == ("pending_restart" if change == "restart" else "active")
        await rebuilt.reload()
        replay = await rebuilt.save_source_edit(source_key=SOURCE, request=request)
        assert replay.edit == saved.edit
        assert len(case.calls) == (1 if change == "hot" else 0)
        events = rebuilt.list_events()
        assert len(events) == (0 if change == "unchanged" else 1)
        assert all(event.candidate_id == accepted.candidate_id for event in events)
    finally:
        await rebuilt.stop()


async def test_unsaved_intent_does_not_block_unrelated_external_source(source_owner: SourceOwner, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.gateway.config import source_edit

    case = source_owner
    request = case.request('{"ui":{"theme":{"default_theme_id":"blue"}}}')

    def fail(_path: Path, _data: bytes) -> None:
        raise RuntimeError("模拟未落盘")

    with monkeypatch.context() as patch:
        patch.setattr(source_edit, "atomic_write_bytes", fail)
        with pytest.raises(RuntimeError, match="未落盘"):
            await case.service.save_source_edit(source_key=SOURCE, request=request)
    case.path.write_text('{"config_version":4,"logging":{"runtime":{"level":"debug"}}}')
    await case.service.reload()
    assert len(case.calls) == 1
    event, = case.service.list_events()
    assert event.idempotency_key != request.idempotency_key and event.changed_paths == ("/logging/runtime/level",)


@pytest.mark.parametrize("capabilities", [(), ("configuration_read",), ("configuration_write",)])
async def test_source_mutations_require_both_permissions_before_owner(
    source_owner: SourceOwner, capabilities: tuple[Capability, ...],
) -> None:
    case = source_owner
    case.allow(*capabilities)
    request = case.request('{"ui":{"theme":{"default_theme_id":"blue"}}}')
    before = case.snapshot()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=case.app), base_url="http://test") as client:
        for method, suffix in (("POST", "/preflight"), ("PUT", "")):
            denied = await client.request(method, "/api/gateway/config/sources/" + SOURCE + suffix, json=request.model_dump())
            assert denied.status_code == 403, denied.text
    assert case.snapshot() == before and not case.calls


async def test_permissions_rechecked_after_waiting_for_reload_lock(source_owner: SourceOwner) -> None:
    case = source_owner
    case.allow("configuration_read", "configuration_write")
    request = case.request('{"ui":{"theme":{"default_theme_id":"blue"}}}')
    before = case.snapshot()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=case.app), base_url="http://test") as client:
        async with case.service._reload_lock:
            writing = asyncio.create_task(client.put("/api/gateway/config/sources/" + SOURCE, json=request.model_dump()))
            await asyncio.sleep(0.05)
            assert not writing.done()
            case.allow("configuration_read")
        denied = await asyncio.wait_for(writing, 2)
    assert denied.status_code == 403 and "configuration_write" in denied.text
    assert case.snapshot() == before and not case.calls


async def test_read_revocation_during_apply_prevents_response_disclosure(source_owner: SourceOwner) -> None:
    case = source_owner
    case.allow("configuration_read", "configuration_write")
    case.blocked = True
    request = case.request('{"ui":{"theme":{"default_theme_id":"blue"}}}')
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=case.app), base_url="http://test") as client:
        writing = asyncio.create_task(client.put("/api/gateway/config/sources/" + SOURCE, json=request.model_dump()))
        await asyncio.wait_for(case.entered.wait(), 2)
        case.allow("configuration_write")
        case.release.set()
        denied = await asyncio.wait_for(writing, 2)
        assert denied.status_code == 403 and '"jsonc"' not in denied.text
        denied_get = await client.get("/api/gateway/config/sources/" + SOURCE)
        assert denied_get.status_code == 403
    assert len(case.calls) == 1 and len(case.service.list_events()) == 1


async def test_start_and_noop_keep_real_runtime_snapshot_identity_before_hot_save(source_owner: SourceOwner) -> None:
    case = source_owner
    case.app.state.gateway_config = case.service.config
    case.service._on_runtime_config = partial(_apply_gateway_runtime_config, case.app)
    initial = case.service.config
    await case.service.start()
    try:
        comment = case.request(case.path.read_text() + "// runtime 身份不因来源注释改变\n", key="comment-runtime")
        saved = await case.service.save_source_edit(source_key=SOURCE, request=comment)
        assert saved.edit is not None and saved.edit.state == "active"
        assert case.app.state.gateway_config is initial and case.service.config is initial
        assert not case.service.list_events()
        request = case.request('{"ui":{"theme":{"default_theme_id":"green"}}}', source=LOCAL, key="hot-runtime")
        applied = await case.service.save_source_edit(source_key=LOCAL, request=request)
        assert applied.edit is not None and applied.edit.state == "active"
        assert case.app.state.gateway_config is case.service.config
        assert case.service.config.default_theme_id == "green"
        assert len(case.service.list_events()) == 1
    finally:
        await case.service.stop()


async def test_recovery_required_source_is_observed_without_reapplying(source_owner: SourceOwner) -> None:
    case = source_owner
    calls = []

    async def fail(
        candidate: GatewayConfig, _previous: GatewayConfig, _fence: str,
        _assert_claim: Callable[[], None],
    ) -> GatewayRuntimeConsumerApplyResult:
        calls.append(candidate.revision)
        raise RuntimeError("模拟外部副作用未完成，必须由原恢复入口处理")

    case.service._on_runtime_config = fail
    request = case.request('{"ui":{"theme":{"default_theme_id":"blue"}}}')
    with pytest.raises(RuntimeError, match="runtime_apply"):
        await case.service.save_source_edit(source_key=SOURCE, request=request)
    pending = case.store.get_pending_config_candidate(config_domain="gateway", idempotency_key=request.idempotency_key)
    assert pending is not None and pending.state == "recovery_required"
    rebuilt = case.rebuild()
    await rebuilt.start()
    try:
        saved = await rebuilt.save_source_edit(source_key=SOURCE, request=request)
        assert saved.edit is not None and saved.edit.candidate_id == pending.candidate_id
        assert saved.edit.state == "recovery_required"
        assert calls == [pending.candidate_digest] and not case.calls
        assert len(rebuilt.list_events()) == 1
    finally:
        await rebuilt.stop()
