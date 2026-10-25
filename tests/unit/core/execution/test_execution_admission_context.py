from __future__ import annotations

import pytest

from app.core.execution_admission_context import (
    current_execution_admission,
    execution_admission_scope,
    require_current_execution_admission,
)
from app.core.session_thread_context import session_thread_owner_scope
from app.domain.itemized.execution_admission import (
    ExecutionAdmission,
    ExecutionOrigin,
)
from app.domain.itemized.mutation_intents import MutationIntentOwner


def _admission(
    *,
    session_id: str = "ses_a",
    thread_id: str = "thr_a",
    execution_id: str = "exe_a",
    execution_origin: ExecutionOrigin = "user_turn",
    turn_id: str | None = "turn_a",
) -> ExecutionAdmission:
    return ExecutionAdmission(
        session_id=session_id,
        thread_id=thread_id,
        execution_id=execution_id,
        job_id="job_a",
        admission_idempotency_key="admit_a",
        execution_origin=execution_origin,
        turn_id=turn_id,
    )


def test_admission_requires_origin_and_turn_binding_to_agree() -> None:
    with pytest.raises(ValueError, match="真实 turn_id"):
        _admission(turn_id=None)
    with pytest.raises(ValueError, match="turn_id 必须为 None"):
        _admission(execution_origin="internal")
    assert _admission(execution_origin="internal", turn_id=None).turn_id is None


def test_scope_requires_matching_session_thread_owner_and_restores_context() -> None:
    admission = _admission()
    owner = MutationIntentOwner(session_id="ses_a", thread_id="thr_a")
    with session_thread_owner_scope(owner, langgraph_thread_id="thr_a"):
        with execution_admission_scope(admission, langgraph_thread_id="thr_a"):
            assert require_current_execution_admission() == admission
            with pytest.raises(ValueError, match="冲突"):
                require_current_execution_admission(langgraph_thread_id="thr_b")
        assert current_execution_admission() is None
    assert current_execution_admission() is None


def test_scope_rejects_admission_for_a_different_session_owner() -> None:
    owner = MutationIntentOwner(session_id="ses_a", thread_id="thr_a")
    admission = _admission(session_id="ses_b")
    with (
        session_thread_owner_scope(owner, langgraph_thread_id="thr_a"),
        pytest.raises(RuntimeError, match="SessionThread owner"),
        execution_admission_scope(admission, langgraph_thread_id="thr_a"),
    ):
        pytest.fail("owner 冲突的 admission 不得进入 scope")


def test_scope_rejects_missing_session_thread_owner() -> None:
    with (
        pytest.raises(RuntimeError, match="缺少显式 SessionThread owner"),
        execution_admission_scope(
            _admission(),
            langgraph_thread_id="thr_a",
        ),
    ):
        pytest.fail("缺少 owner 的 admission 不得进入 scope")
