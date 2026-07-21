from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone

from app.services.infrastructure.config.snapshot import (
    ConfigReloadFailureReason,
    ConfigReloadStatus,
    ConfigRestartRequiredError,
    ConfigSnapshot,
)

logger = logging.getLogger(__name__)

ConfigCandidateBuilder = Callable[[], ConfigSnapshot]
ConfigCandidateApplier = Callable[[ConfigSnapshot, ConfigSnapshot], Awaitable[None]]


class ConfigSnapshotStore:
    def __init__(self, *, candidate_builder: ConfigCandidateBuilder) -> None:
        self._candidate_builder = candidate_builder
        self._snapshot: ConfigSnapshot | None = None
        self._status: ConfigReloadStatus | None = None

    def initialize(self, candidate: ConfigSnapshot | None = None) -> None:
        self._commit(candidate or self._candidate_builder())

    def has_snapshot(self) -> bool:
        return self._snapshot is not None

    def current(self) -> ConfigSnapshot:
        if self._snapshot is None:
            self.initialize()
        if self._snapshot is None:
            raise RuntimeError("配置快照初始化失败")
        return self._snapshot

    def status(self) -> ConfigReloadStatus:
        self.current()
        if self._status is None:
            raise RuntimeError("配置热重载状态尚未初始化")
        return self._status

    async def reload(
        self,
        *,
        candidate_applier: ConfigCandidateApplier | None = None,
    ) -> bool:
        previous = self.current()
        try:
            candidate = self._candidate_builder()
        except Exception as error:
            self._record_failure(error, reason="invalid_config")
            raise
        if candidate.revision == previous.revision:
            self._commit(candidate)
            return False
        if candidate_applier is not None:
            try:
                await candidate_applier(previous, candidate)
            except Exception as error:
                self._record_failure(
                    error,
                    reason=(
                        "restart_required"
                        if isinstance(error, ConfigRestartRequiredError)
                        else "apply_failed"
                    ),
                )
                raise
        self._commit(candidate)
        logger.info("配置热重载成功: revision=%s", candidate.revision)
        return True

    def _record_failure(
        self,
        error: Exception,
        *,
        reason: ConfigReloadFailureReason,
    ) -> None:
        now = datetime.now(timezone.utc)
        active = self.current()
        previous_status = self._status
        restart_required = isinstance(error, ConfigRestartRequiredError)
        self._status = ConfigReloadStatus(
            healthy=False,
            revision=active.revision,
            last_success_at=(
                previous_status.last_success_at
                if previous_status is not None
                else active.loaded_at
            ),
            last_attempt_at=now,
            last_error=f"{type(error).__name__}: {error}",
            restart_required=restart_required,
            reason=reason,
            changed_sections=(
                error.changed_sections
                if isinstance(error, ConfigRestartRequiredError)
                else ()
            ),
        )
        logger.exception(
            "配置热重载失败，继续使用最后一个有效快照: revision=%s",
            active.revision,
        )

    def _commit(self, candidate: ConfigSnapshot) -> None:
        # revision 表示最终有效配置内容；即使内容相同，也要刷新来源路径和加载时间，
        # 让元数据准确反映 json/jsonc 优先级切换后的当前来源。
        self._snapshot = candidate
        active = self.current()
        now = datetime.now(timezone.utc)
        self._status = ConfigReloadStatus(
            healthy=True,
            revision=active.revision,
            last_success_at=now,
            last_attempt_at=now,
            last_error=None,
            restart_required=False,
            reason=None,
            changed_sections=(),
        )
