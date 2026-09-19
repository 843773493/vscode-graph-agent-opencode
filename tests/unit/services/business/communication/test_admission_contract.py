"""跨 Session send admission 合同测试：main binding、幂等冲突与 reply 因果。"""

from __future__ import annotations

import uuid

import pytest

from app.services.business.communication.addresses import (
    GlobalThreadAddress,
    ResolvedMainTargetBinding,
)
from app.services.business.communication.admission import (
    CommunicationPayload,
    CommunicationSendRequest,
    ExistingOutboxView,
    ReceivedCommunicationView,
    decide_send_admission,
    resolve_request_preimage,
)
from app.services.business.communication.errors import CommunicationContractError


def make_address() -> GlobalThreadAddress:
    """构造合法 canonical 地址；session/thread 使用真实 uuid4 hex。"""
    return GlobalThreadAddress(
        gateway_id="gw_local",
        workspace_id="ws_main",
        session_id=f"ses_{uuid.uuid4().hex}",
        thread_id=f"thr_{uuid.uuid4().hex}",
    )


def make_request(
    *,
    source: GlobalThreadAddress | None = None,
    target: GlobalThreadAddress | None = None,
    operation_id: str = "op-1",
    payload: CommunicationPayload | None = None,
) -> CommunicationSendRequest:
    resolved_target = target or make_address()
    return CommunicationSendRequest(
        source=source or make_address(),
        target_binding=ResolvedMainTargetBinding(
            target_session_id=resolved_target.session_id,
            target_main_thread_id=resolved_target.thread_id,
            target=resolved_target,
        ),
        send_operation_id=operation_id,
        payload=payload or CommunicationPayload(content="你好"),
    )


def make_existing(
    request: CommunicationSendRequest,
    *,
    communication_id: str = "comm_existing",
) -> ExistingOutboxView:
    return ExistingOutboxView(
        source=request.source,
        send_operation_id=request.send_operation_id,
        communication_id=communication_id,
        target=request.target_binding.target,
        payload_hash=resolve_request_preimage(request),
    )


def test_new_admission_binds_candidate_communication_id() -> None:
    request = make_request()
    decision = decide_send_admission(
        request=request, communication_id="comm_new", existing=None
    )
    assert decision.communication_id == "comm_new"
    assert decision.is_duplicate is False
    assert decision.payload_hash.startswith("sha256:")


def test_global_thread_address_rejects_non_canonical_ids() -> None:
    with pytest.raises(CommunicationContractError):
        GlobalThreadAddress(
            gateway_id="gw_local",
            workspace_id="ws_main",
            session_id="ses_not-canonical",
            thread_id=f"thr_{uuid.uuid4().hex}",
        )


def test_target_binding_rejects_child_thread_as_target() -> None:
    target = make_address()
    with pytest.raises(CommunicationContractError, match="communication-target-not-main"):
        ResolvedMainTargetBinding(
            target_session_id=target.session_id,
            target_main_thread_id=f"thr_{uuid.uuid4().hex}",
            target=target,
        )


def test_target_binding_accepts_consistent_main_pointer() -> None:
    target = make_address()
    binding = ResolvedMainTargetBinding(
        target_session_id=target.session_id,
        target_main_thread_id=target.thread_id,
        target=target,
    )
    assert binding.target.thread_id == target.thread_id


def test_payload_rejects_empty_content_and_reply_field_misuse() -> None:
    with pytest.raises(CommunicationContractError, match="communication-payload-invalid"):
        CommunicationPayload(content="   ")
    with pytest.raises(CommunicationContractError, match="communication-payload-invalid"):
        CommunicationPayload(content="问题", kind="reply")
    with pytest.raises(CommunicationContractError, match="communication-payload-invalid"):
        CommunicationPayload(content="结果", reply_to_communication_id="comm_1")


def test_preimage_hash_is_deterministic() -> None:
    request = make_request()
    assert resolve_request_preimage(request) == resolve_request_preimage(request)


def test_same_operation_same_preimage_returns_original_receipt() -> None:
    request = make_request()
    existing = make_existing(request, communication_id="comm_first")
    decision = decide_send_admission(
        request=request, communication_id="comm_first", existing=existing
    )
    assert decision.is_duplicate is True
    assert decision.communication_id == "comm_first"


def test_same_operation_different_payload_conflicts() -> None:
    request = make_request()
    existing = make_existing(request)
    retry = make_request(
        source=request.source,
        target=request.target_binding.target,
        operation_id=request.send_operation_id,
        payload=CommunicationPayload(content="改写后的内容"),
    )
    with pytest.raises(CommunicationContractError, match="communication-preimage-conflict"):
        decide_send_admission(request=retry, communication_id="comm_x", existing=existing)


def test_same_operation_different_target_conflicts() -> None:
    request = make_request()
    existing = make_existing(request)
    retry = make_request(
        source=request.source,
        target=make_address(),
        operation_id=request.send_operation_id,
    )
    with pytest.raises(CommunicationContractError, match="communication-preimage-conflict"):
        decide_send_admission(request=retry, communication_id="comm_x", existing=existing)


def test_same_communication_id_same_preimage_dedupes_across_operations() -> None:
    request = make_request(operation_id="op-2")
    existing = make_existing(request, communication_id="comm_shared")
    decision = decide_send_admission(
        request=request, communication_id="comm_shared", existing=existing
    )
    assert decision.is_duplicate is True
    assert decision.communication_id == "comm_shared"


def test_same_communication_id_different_payload_conflicts() -> None:
    request = make_request(operation_id="op-2")
    existing = make_existing(request, communication_id="comm_shared")
    other = make_request(
        source=request.source,
        target=request.target_binding.target,
        operation_id="op-3",
        payload=CommunicationPayload(content="另一条消息"),
    )
    with pytest.raises(CommunicationContractError, match="communication-id-conflict"):
        decide_send_admission(
            request=other, communication_id="comm_shared", existing=existing
        )


def test_reply_requires_reverse_direction_proof() -> None:
    inbound = ReceivedCommunicationView(
        communication_id="comm_q",
        source=make_address(),
        target=make_address(),
    )
    reply = make_request(
        source=inbound.target,
        target=inbound.source,
        payload=CommunicationPayload(
            content="回复", kind="reply", reply_to_communication_id="comm_q"
        ),
    )
    with pytest.raises(
        CommunicationContractError, match="communication-reply-correlation-conflict"
    ):
        decide_send_admission(
            request=reply, communication_id="comm_reply", existing=None
        )
    decision = decide_send_admission(
        request=reply,
        communication_id="comm_reply",
        existing=None,
        reply_context=inbound,
    )
    assert decision.is_duplicate is False


def test_reply_direction_mismatch_conflicts() -> None:
    inbound = ReceivedCommunicationView(
        communication_id="comm_q",
        source=make_address(),
        target=make_address(),
    )
    # 同方向重放（source/target 与收件视图相同）必须被拒绝。
    replay = make_request(
        source=inbound.source,
        target=inbound.target,
        payload=CommunicationPayload(
            content="伪造回复", kind="reply", reply_to_communication_id="comm_q"
        ),
    )
    with pytest.raises(
        CommunicationContractError, match="communication-reply-correlation-conflict"
    ):
        decide_send_admission(
            request=replay,
            communication_id="comm_reply",
            existing=None,
            reply_context=inbound,
        )
