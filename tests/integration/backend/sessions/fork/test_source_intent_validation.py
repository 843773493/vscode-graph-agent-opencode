"""畸形来源回执不得把原生位置复制到普通错误，修正后仍复用原创建意图。"""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from app.container import AppContainer
from app.core.resource_uri import thread_resource_uri
from app.core.trace_middleware import (
    reset_current_gateway_id,
    reset_current_request_id,
    set_current_gateway_id,
    set_current_request_id,
)
from app.schemas.internal_v2.session_fork_transfer import (
    SessionForkTransferRequest,
    SessionForkTransferResultDTO,
)
from app.services.business.session_context_resource import (
    workspace_session_inventory_uri,
)
from tests.integration.backend.sessions.test_rollout_fork_modes import (
    SourceForkOwnerTransport,
    _fork_intent_state,
    _fork_request,
    _main_thread_id,
    _production_fork_context,
    _seed_source,
)
from tests.integration.backend.sessions.test_rollout_fork_modes import (
    fork_gateway_request_context as fork_gateway_request_context,  # noqa: PLC0414
)
from tests.integration.backend.sessions.test_rollout_fork_modes import (
    fork_provider_prepare as fork_provider_prepare,  # noqa: PLC0414
)
from tests.integration.backend.sessions.test_rollout_fork_modes import (
    production_fork_container as production_fork_container,  # noqa: PLC0414
)
from tests.integration.backend.sessions.test_rollout_fork_modes import (
    remote_fork_target as remote_fork_target,  # noqa: PLC0414
)
from tests.integration.backend.sessions.test_rollout_fork_modes import (
    tool_set as tool_set,  # noqa: PLC0414
)


@pytest.mark.parametrize("native_path", [
    "/private/fork/source.json", r"C:\private\fork\source.json", r"\\source\private\fork\source.json",
])
async def test_invalid_source_intent_preserves_diagnostics_and_retries_original_operation(
    production_fork_container: AppContainer,
    remote_fork_target: tuple[AppContainer, SourceForkOwnerTransport],
    fork_provider_prepare,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    native_path: str,
) -> None:
    source = _production_fork_context(production_fork_container)
    session = await _seed_source(source, fork_provider_prepare)
    target_container, transport = remote_fork_target
    target = _production_fork_context(target_container)
    source_gateway = "gateway_4aae160f813ed87bcf65a8bb8bfd3729"
    target_gateway = "gateway_2d2ab24c1f08e28f5f0217140c8be744"
    request = (await _fork_request(
        source, session.session_id, creation_kind="new_session_main", mode="full_rollout_copy",
    )).model_copy(update={
        "source_uri": thread_resource_uri(
            session.workspace_id, session.session_id, _main_thread_id(source, session.session_id),
            gateway_authority=source_gateway,
        ),
        "target_container_uri": workspace_session_inventory_uri(
            target_container.workspace_id, gateway_authority=target_gateway,
        ),
    })
    key = target.forks._creation_key(request.idempotency_key)
    original_transfer = transport.fork_transfer

    async def malformed_intent(payload: SessionForkTransferRequest) -> SessionForkTransferResultDTO:
        reply = await original_transfer(payload)
        if payload.action != "intent":
            return reply
        assert reply.intent is not None
        # 模拟 wire 合法、业务字段类型非法的远端回执；不改来源持久事实。
        intent = {**reply.intent, "display_title": {"native_path": native_path}}
        return SessionForkTransferResultDTO.model_validate({"intent": intent})

    source_before, target_before = _fork_intent_state(source), _fork_intent_state(target)
    gateway_token = set_current_gateway_id(target_gateway)
    request_token = set_current_request_id("source-intent-validation")
    try:
        monkeypatch.setattr(transport, "fork_transfer", malformed_intent)
        try:
            with pytest.raises(RuntimeError, match="fork_response_protocol_invalid") as caught:
                await target.forks.fork(request)
            assert isinstance(caught.value.__cause__, ValidationError)
            assert caught.value.__cause__.errors()[0]["input"] == {"native_path": native_path}
            public_error = json.dumps({"error": str(caught.value)})
            assert "private" not in public_error
            assert "native_path" not in public_error
            assert all(value in str(caught.value) for value in (
                f"operation_id={key}", f"gateway_id={source_gateway}",
                f"workspace_id={session.workspace_id}", "stage=source_intent_validation",
                "error=ValidationError",
            ))
            assert any(record.exc_info and record.exc_info[1] is caught.value.__cause__ for record in caplog.records)
            assert _fork_intent_state(source) == source_before
            assert _fork_intent_state(target) == target_before
            assert [call.action for call in transport.calls] == ["intent"]
            with pytest.raises(KeyError):
                target.sessions.path_resolver.catalog_store.get_creation_record(key)
            assert target.sessions.path_resolver.catalog_store.get_fork_source_capture(copy_operation_id=key) is None
            assert await target_container.job_service.list() == []
        finally:
            # 即使反例断言失败，也让真实 owner 用原 key 完成操作，保留原 deadline 的证明能力。
            monkeypatch.setattr(transport, "fork_transfer", original_transfer)
            result = await target.forks.fork(request)
            assert result.export_release is not None and result.export_release.state == "confirmed"
        record = target.sessions.path_resolver.catalog_store.get_creation_record(key)
        assert record.state == "published" and record.session_id == result.session.session_id
        replay = await target.forks.fork(request)
        assert replay == result
        assert target.sessions.path_resolver.catalog_store.get_creation_record(key) == record
        assert sum(call.action == "capture" for call in transport.calls) == 1
        assert await target_container.job_service.list() == []
    finally:
        reset_current_request_id(request_token)
        reset_current_gateway_id(gateway_token)
