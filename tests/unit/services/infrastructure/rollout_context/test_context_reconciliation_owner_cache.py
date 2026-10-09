from __future__ import annotations

from dataclasses import dataclass
from threading import RLock
from types import SimpleNamespace
from typing import Self

import pytest

from app.services.infrastructure.rollout_context.checkpoint.owner.context_reconciliation import (
    ContextReconciliationMixin,
)

SESSION_ID = "ses_01900000000070008000000000000001"
MAIN_THREAD_ID = "thr_01900000000070008000000000000002"
CHILD_THREAD_ID = "thr_01900000000070008000000000000003"
CHECKPOINT_NS = "shared-namespace"


@dataclass(frozen=True, slots=True)
class _Manifest:
    history_view_revision: int
    source_overlay_epoch: int


class _Snapshot:
    def __init__(
        self,
        session_id: str,
        thread_id: str,
        checkpoint_ns: str,
        manifest: _Manifest,
    ) -> None:
        self.session_id = session_id
        self.thread_id = thread_id
        self.checkpoint_ns = checkpoint_ns
        self.manifest = manifest

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        return None


class _Reader:
    def __init__(self) -> None:
        self.opened: list[tuple[str, str, str]] = []
        self._manifests = {
            (SESSION_ID, MAIN_THREAD_ID, CHECKPOINT_NS): _Manifest(4, 3),
            (SESSION_ID, CHILD_THREAD_ID, CHECKPOINT_NS): _Manifest(11, 8),
        }

    def open_snapshot(
        self,
        session_id: str,
        thread_id: str,
        checkpoint_ns: str,
    ) -> _Snapshot:
        owner = (session_id, thread_id, checkpoint_ns)
        self.opened.append(owner)
        return _Snapshot(*owner, self._manifests[owner])


class _Assemblies:
    def list_source_overlays(
        self,
        session_id: str,
        thread_id: str,
        *,
        checkpoint_ns: str,
        snapshot: _Snapshot,
    ) -> list[dict[str, str]]:
        assert (snapshot.session_id, snapshot.thread_id, snapshot.checkpoint_ns) == (
            session_id,
            thread_id,
            checkpoint_ns,
        )
        return [
            {
                "status": "active",
                "base_ref": f"base:{thread_id}",
                "delta_ref": f"delta:{thread_id}",
                "source_revision": f"revision:{thread_id}",
            }
        ]


class _Saver(ContextReconciliationMixin):
    def __init__(self) -> None:
        self._lock = RLock()
        self._context_reader = _Reader()
        self._storage = SimpleNamespace(assemblies=_Assemblies())
        self._context_reconciliations = {}


@pytest.fixture
def saver() -> _Saver:
    return _Saver()


def test_reconciliation_cache_is_scoped_to_session_thread_and_namespace(
    saver: _Saver,
) -> None:
    main_state = saver.get_context_reconciliation(
        SESSION_ID,
        MAIN_THREAD_ID,
        checkpoint_ns=CHECKPOINT_NS,
    )
    child_state = saver.get_context_reconciliation(
        SESSION_ID,
        CHILD_THREAD_ID,
        checkpoint_ns=CHECKPOINT_NS,
    )

    assert (main_state.history_view_revision, main_state.source_overlay_epoch) == (
        4,
        3,
    )
    assert (child_state.history_view_revision, child_state.source_overlay_epoch) == (
        11,
        8,
    )
    assert main_state.base_ref == f"base:{MAIN_THREAD_ID}"
    assert child_state.base_ref == f"base:{CHILD_THREAD_ID}"
    assert main_state.source_revision_set == (f"revision:{MAIN_THREAD_ID}",)
    assert child_state.source_revision_set == (f"revision:{CHILD_THREAD_ID}",)
    assert set(saver._context_reconciliations) == {
        (SESSION_ID, MAIN_THREAD_ID, CHECKPOINT_NS),
        (SESSION_ID, CHILD_THREAD_ID, CHECKPOINT_NS),
    }

    assert (
        saver.get_context_reconciliation(
            SESSION_ID,
            MAIN_THREAD_ID,
            checkpoint_ns=CHECKPOINT_NS,
        )
        is main_state
    )
    assert (
        saver.get_context_reconciliation(
            SESSION_ID,
            CHILD_THREAD_ID,
            checkpoint_ns=CHECKPOINT_NS,
        )
        is child_state
    )
    assert saver._context_reader.opened == [
        (SESSION_ID, MAIN_THREAD_ID, CHECKPOINT_NS),
        (SESSION_ID, CHILD_THREAD_ID, CHECKPOINT_NS),
    ]
