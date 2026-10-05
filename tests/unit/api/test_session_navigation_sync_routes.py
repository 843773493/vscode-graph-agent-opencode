"""会话目录写入仅公开 durable navigation intent。"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.api.session_navigation import router as navigation_router
from app.schemas.internal_v2.session import SessionCreateRequest, SessionUpdateRequest
from app.schemas.internal_v2.session_navigation.operations import (
    NavigationMutationEnqueueRequest,
    NavigationMutationIntentDTO,
)


def test_legacy_synchronous_navigation_writers_are_not_registered() -> None:
    navigation_paths = {route.path for route in navigation_router.routes}
    assert "/session-catalog/folders" not in navigation_paths
    assert "/session-catalog/folders/{folder_id}" not in navigation_paths
    assert "/session-catalog/sessions/{session_id}/folder" not in navigation_paths
    assert "/session-catalog/nodes/{node_id}/parent" not in navigation_paths
    assert "/session-catalog/sessions/{session_id}" not in navigation_paths


def test_session_update_is_limited_to_agent_and_provider() -> None:
    with pytest.raises(ValidationError):
        SessionUpdateRequest.model_validate({"title": "同步改名不得接受"})

    with pytest.raises(ValidationError):
        SessionCreateRequest.model_validate(
            {"title": "公开创建", "title_source": "auto"}
        )


def test_public_rename_is_a_revision_pinned_navigation_intent() -> None:
    intent = NavigationMutationIntentDTO.model_validate(
        {
            "client_operation_id": "op_" + "a" * 32,
            "client_sequence": 1,
            "kind": "rename_node",
            "base_catalog_revision": 7,
            "expected_revision": 3,
            "target_node_id": "ses_00000000000070008000000000000000",
            "name": "队列改名",
        }
    )
    assert intent.kind == "rename_node"
    assert intent.name == "队列改名"
    assert intent.expected_revision == 3


def test_enqueue_contract_has_no_batch_id_or_title_source() -> None:
    intent = {
        "client_operation_id": "op_" + "b" * 32,
        "client_sequence": 1,
        "kind": "rename_node",
        "base_catalog_revision": 7,
        "expected_revision": 3,
        "target_node_id": "ses_00000000000070008000000000000000",
        "name": "队列改名",
    }
    with pytest.raises(ValidationError):
        NavigationMutationEnqueueRequest.model_validate(
            {"batch_id": "b_legacy", "intents": [intent]}
        )
    with pytest.raises(ValidationError):
        NavigationMutationIntentDTO.model_validate(
            {**intent, "title_source": "user"}
        )
