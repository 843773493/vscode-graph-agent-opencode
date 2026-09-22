from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace

from app.services.infrastructure.config.snapshot import ConfigReloadStatus
from app.services.infrastructure.workspace_state_store import WorkspaceStateStore


class ReloadStatusCoordinator:
    """把 Workspace 状态库的 active/pending 状态叠加到快照重载状态之上。"""

    def __init__(
        self,
        *,
        store: WorkspaceStateStore | None,
        config_domain: str,
        snapshot_status_provider: Callable[[], ConfigReloadStatus],
    ) -> None:
        self._store = store
        self._config_domain = config_domain
        self._snapshot_status_provider = snapshot_status_provider

    def get_reload_status(self) -> ConfigReloadStatus:
        status = self._snapshot_status_provider()
        if self._store is None:
            return status
        active = self._store.get_active_config_snapshot(
            self._config_domain
        )
        pending = self._store.get_pending_config_candidate(
            config_domain=self._config_domain
        )
        if active is None and pending is None:
            return status
        pending_state = pending.state if pending is not None else None
        reason = status.reason
        if pending_state == "discarded":
            reason = None
        elif pending_state in {"conflict", "rejected", "recovery_required"}:
            reason = pending_state
        elif pending_state == "pending_restart":
            reason = "restart_required"
        return replace(
            status,
            healthy=(
                (status.healthy or pending_state == "discarded")
                and pending_state
                not in {"conflict", "rejected", "recovery_required"}
            ),
            restart_required=pending_state == "pending_restart",
            reason=reason,
            state=pending_state or ("active" if active is not None else None),
            active_revision=active.active_revision if active is not None else None,
            pending_revision=pending.pending_revision if pending is not None else None,
            candidate_id=pending.candidate_id if pending is not None else None,
            candidate_ref=pending.candidate_ref if pending is not None else None,
            attempt_id=pending.last_attempt_id if pending is not None else None,
            apply_id=pending.last_apply_id if pending is not None else None,
            layer_digests=active.layer_digests if active is not None else None,
            last_error=(
                pending.last_error
                if pending is not None and pending.last_error is not None
                else status.last_error
            ),
        )
