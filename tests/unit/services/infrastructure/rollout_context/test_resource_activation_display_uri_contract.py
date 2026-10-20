"""activation display_uri 的 SQLite 与冷读拒绝合同。"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from dataclasses import replace
from pathlib import Path

import pytest

from app.services.infrastructure.resource_platform.derivation.types import (
    ResourceSnapshot,
    SemanticResourceDescriptor,
)
from app.services.infrastructure.rollout_context.activation.lineage import (
    ActivationLineageBodyStore,
)
from app.services.infrastructure.rollout_context.activation.store import (
    ResourceActivationStore,
)
from app.services.infrastructure.rollout_context.checkpoint.message_codec import (
    LangChainMessageCodec,
)
from app.services.infrastructure.rollout_context.runtime.detail_store import (
    ContextPlanDetailStore,
)
from app.services.infrastructure.rollout_context.store import RolloutContextStore
from tests.support.catalog_session_bundle import seed_catalog_session_bundle
from tests.unit.services.infrastructure.rollout_context.test_resource_activation_storage import (
    _execution_snapshot,
)

SESSION_ID = "ses_019c13b18bcc7288beace1cd3b7c4c84"


@pytest.mark.parametrize("value", ["", "https://provider.invalid/skill", "boxteam://workspace/ws/.boxteam/skills/demo/SKILL.md"])
def test_internal_aggregate_display_uri_rejects_empty_and_illegal_values(value: str) -> None:
    with pytest.raises(ValueError):
        SemanticResourceDescriptor(
            resource_id="skill:demo:catalog", resource_kind="skill", facet="catalog", display_uri=value,
        )
    with pytest.raises(ValueError):
        ResourceSnapshot(
            resource_id="skill:demo:catalog", resource_kind="skill", facet="catalog",
            display_uri=value, revision="sha256:jcs:v1:" + "a" * 64, payload={},
            source_lineage=(), generation=0,
        )


@pytest.fixture
def sessions_root(tmp_path: Path) -> Path:
    root = tmp_path / ".boxteam" / "sessions"
    root.mkdir(parents=True)
    seed_catalog_session_bundle(root, SESSION_ID)
    return root


@pytest.fixture
def storage(sessions_root: Path) -> RolloutContextStore:
    instance = RolloutContextStore(sessions_root, message_codec=LangChainMessageCodec())
    instance.storage.initialize(SESSION_ID, validate_jsonl_items=False)
    return instance


@pytest.fixture
def activation_store(
    sessions_root: Path, storage: RolloutContextStore,
) -> ResourceActivationStore:
    details = ContextPlanDetailStore(sessions_root)
    return ResourceActivationStore(storage, ActivationLineageBodyStore(storage, details))


def test_activation_sqlite_round_trip_preserves_explicit_null_display_uri(
    activation_store: ResourceActivationStore,
    storage: RolloutContextStore,
) -> None:
    original = _execution_snapshot(storage=storage)
    binding = replace(original.bindings[0], display_uri=None)
    snapshot = replace(original, bindings=(binding,))

    activation_store.save_snapshot(snapshot)
    restored = activation_store.read_snapshot(
        snapshot.owner_session_id,
        thread_id=snapshot.owner_thread_id,
        activation_snapshot_id=snapshot.activation_snapshot_id,
    )

    assert restored.bindings[0].display_uri is None
    assert restored.bindings_hash == snapshot.bindings_hash


def test_activation_sqlite_rejects_empty_display_uri_and_cold_read_rejects_tampering(
    activation_store: ResourceActivationStore,
    storage: RolloutContextStore,
) -> None:
    snapshot = _execution_snapshot(storage=storage)
    activation_store.save_snapshot(snapshot)
    index_path = storage.storage.index_path(
        snapshot.owner_session_id, "",
    )

    with closing(sqlite3.connect(index_path)) as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE resource_activation_bindings SET display_uri = '' "
                "WHERE activation_snapshot_id = ?",
                (snapshot.activation_snapshot_id,),
            )
        connection.rollback()

        connection.execute("PRAGMA ignore_check_constraints = ON")
        connection.execute(
            "UPDATE resource_activation_bindings SET display_uri = '' "
            "WHERE activation_snapshot_id = ?",
            (snapshot.activation_snapshot_id,),
        )
        connection.commit()

    with pytest.raises(RuntimeError, match="display_uri 必须是非空字符串"):
        activation_store.read_snapshot(
            snapshot.owner_session_id,
            thread_id=snapshot.owner_thread_id,
            activation_snapshot_id=snapshot.activation_snapshot_id,
        )
