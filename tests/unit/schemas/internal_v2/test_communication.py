"""通信观察边界必须完整冻结预算与 selector，拒绝额外字段和隐式强转。"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from app.schemas.internal_v2.communication import CommunicationObservationRequest
from app.services.business.communication.addresses import GlobalThreadAddress
from app.services.business.communication.errors import CommunicationContractError
from tests.support.communication.probe import (
    SOURCE_SESSION,
    SOURCE_THREAD,
    TARGET_SESSION,
    TARGET_THREAD,
    WORKSPACE_ID,
)


@pytest.fixture
def wait_body() -> dict[str, object]:
    admitted = datetime(2026, 10, 7, tzinfo=UTC)
    return {
        "observation_id": "obs_contract",
        "source_call_id": "cop_contract",
        "page_identity": "page_contract",
        "page_ordinal": 0,
        "expires_at": (admitted + timedelta(days=1)).isoformat().replace("+00:00", "Z"),
        "operation": "wait",
        "terminal_replay": False,
        "source": asdict(
            GlobalThreadAddress(
                "gateway_source", WORKSPACE_ID, SOURCE_SESSION, SOURCE_THREAD
            )
        ),
        "target": asdict(
            GlobalThreadAddress(
                "gateway_target", WORKSPACE_ID, TARGET_SESSION, TARGET_THREAD
            )
        ),
        "query": {
            "selector": {"kind": "job", "selector_id": "job_contract"},
            "until": "terminal",
            "timeout_seconds": 1,
            "deadline": {
                "timeout_seconds": 1,
                "admitted_at_utc": admitted.isoformat(),
                "deadline_at_utc": (admitted + timedelta(seconds=1)).isoformat(),
                "monotonic_origin_id": "boot_contract",
                "monotonic_deadline_ns": 1_000_000_000,
            },
        },
    }


def test_wait_contract_preserves_exact_wire(wait_body: dict[str, object]) -> None:
    assert (
        CommunicationObservationRequest.model_validate(wait_body).model_dump(
            mode="json"
        )
        == wait_body
    )


@pytest.mark.parametrize(
    "part,field,value",
    [
        ("deadline", "extra", "ignored"),
        ("deadline", "timeout_seconds", "1"),
        ("deadline", "timeout_seconds", True),
        ("deadline", "monotonic_deadline_ns", True),
        ("deadline", "monotonic_deadline_ns", "1000000000"),
        ("deadline", "monotonic_origin_id", 1),
        ("selector", "extra", "ignored"),
        ("selector", "selector_id", 1),
        ("selector", "kind", "unknown"),
    ],
)
def test_wait_contract_rejects_extra_and_coercion(
    wait_body: dict[str, object], part: str, field: str, value: object
) -> None:
    changed = deepcopy(wait_body)
    changed["query"][part][field] = value
    with pytest.raises((ValidationError, CommunicationContractError)):
        CommunicationObservationRequest.model_validate(changed)


@pytest.mark.parametrize(
    "field,value",
    [
        ("timeout_seconds", True),
        ("timeout_seconds", "1"),
        ("timeout_seconds", 2),
        ("until", "idle"),
        ("selector", False),
    ],
)
def test_wait_query_rejects_conflicting_budget_and_selector(
    wait_body: dict[str, object], field: str, value: object
) -> None:
    changed = deepcopy(wait_body)
    changed["query"][field] = value
    with pytest.raises(ValidationError):
        CommunicationObservationRequest.model_validate(changed)
