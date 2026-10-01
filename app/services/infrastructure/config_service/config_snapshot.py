"""ConfigSnapshotMixin：ConfigService 的 snapshot 构建、读取与 pending-restart 契约方法族（纯搬迁）。"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import jsonschema

from app.core.config_sources import (
    ConfigSource,
    ConfigSourceLayer,
)
from app.core.path_utils import (
    get_workspace_config_path,
)
from app.services.infrastructure.config import (
    ConfigReloadStatus,
    ConfigSnapshot,
    build_config_snapshot,
)
from app.services.infrastructure.config.state import (
    ConfigActiveSnapshotRecord,
    ConfigConflictError,
    ConfigPendingCandidateRecord,
    SecretReferenceRequiredError,
    build_secret_binding_summary,
    prepare_config_for_persistence,
    restore_environment_secret_references,
)
from app.services.infrastructure.config_service.config_service_common import (
    _SOURCE_LAYER_AUTHORITY,
)


class ConfigSnapshotMixin:
    def _build_candidate_snapshot(
        self,
        *,
        validate_schema: bool = True,
    ) -> ConfigSnapshot:
        config, source_paths, source_details = self._read_effective_config()
        schema_path = self._resolve_schema_path()
        if validate_schema:
            jsonschema.validate(config, self._load_schema())
        snapshot = build_config_snapshot(
            config,
            source_paths=source_paths,
            source_details=source_details,
            schema_path=schema_path,
        )
        return snapshot

    def validate_workspace_config(self) -> None:
        snapshot = self._require_snapshot()
        jsonschema.validate(snapshot.to_dict(), self._load_schema())
        self._validate_agent_tool_policies(snapshot.to_dict())
        if self._workspace_state_store is None:
            return
        active = self._workspace_state_store.get_active_config_snapshot(
            self._CONFIG_DOMAIN
        )
        if active is None and self._loaded_source == "source":
            self._persist_initial_active_snapshot()

    def _require_snapshot(self) -> ConfigSnapshot:
        pinned = self._pinned_snapshot.get()
        if pinned is not None:
            return pinned
        if not self._snapshot_store.has_snapshot():
            candidate_ref = os.environ.get(self._CANDIDATE_REF_ENV, "").strip()
            if candidate_ref:
                self._snapshot_store.initialize(
                    self._build_startup_pending_snapshot(candidate_ref),
                )
            elif self._workspace_state_store is not None:
                blocked = self._workspace_state_store.migrate_legacy_active_snapshot_secrets(
                    config_domain=self._CONFIG_DOMAIN
                )
                if blocked:
                    raise SecretReferenceRequiredError(
                        "旧 active snapshot 含无法恢复的秘密摘要，必须重新导入: "
                        + ", ".join(blocked)
                    )
                active = self._workspace_state_store.get_active_config_snapshot(
                    self._CONFIG_DOMAIN
                )
                if active is not None:
                    if active.state != "active":
                        raise ConfigConflictError(
                            "active snapshot 当前需要恢复，禁止从损坏记录启动: "
                            f"state={active.state}, error={active.last_error}"
                        )
                    self._snapshot_store.initialize(
                        self._build_persisted_snapshot(active, loaded_source="active")
                    )
                else:
                    # 首次启动没有 active snapshot 时才从 JSONC/source layers
                    # 建立初始快照；后续启动不得因为 source 已有 pending 就越过 active。
                    self._loaded_source = "source"
                    self._snapshot_store.initialize(
                        self._build_candidate_snapshot(validate_schema=False),
                    )
            else:
                self._loaded_source = "source"
                self._snapshot_store.initialize(
                    self._build_candidate_snapshot(validate_schema=False),
                )
        return self._snapshot_store.current()

    def _build_startup_pending_snapshot(self, candidate_ref: str) -> ConfigSnapshot:
        if self._workspace_state_store is None:
            raise ConfigConflictError(
                "Workspace candidate_ref 只能由 Workspace-owned SQLite loader 处理"
            )
        pending = self._workspace_state_store.load_pending_config_candidate(
            candidate_ref=candidate_ref
        )
        generation = os.environ.get(self._GENERATION_ENV, "").strip()
        fencing_token = os.environ.get(self._FENCING_TOKEN_ENV, "").strip()
        if not generation or not fencing_token:
            raise ConfigConflictError(
                "Workspace pending 启动缺少 generation 或 fencing token"
            )
        if pending.target_generation != generation:
            raise ConfigConflictError(
                "Workspace pending candidate 的 target generation 不匹配: "
                f"expected={pending.target_generation}, actual={generation}"
            )
        if pending.fencing_token != fencing_token:
            raise ConfigConflictError("Workspace pending candidate 的 fencing token 不匹配")
        return self._build_persisted_snapshot(pending, loaded_source="pending")

    def _build_persisted_snapshot(
        self,
        record: object,
        *,
        loaded_source: str,
    ) -> ConfigSnapshot:
        if not isinstance(record, (ConfigActiveSnapshotRecord, ConfigPendingCandidateRecord)):
            raise TypeError("持久化配置记录类型无效")
        prepare_config_for_persistence(
            record.payload,
            # 普通 active 恢复先保持旧运行时；只有用户确认的 pending
            # generation 才必须在启动证明阶段解析全部 secret reference。
            resolve_environment=loaded_source == "pending",
        )
        restored = restore_environment_secret_references(record.payload)
        if not isinstance(restored, dict):
            raise TypeError("持久化配置恢复结果必须是对象")
        jsonschema.validate(restored, self._load_schema())
        self._validate_agent_tool_policies(restored)
        source_details, source_paths = self._persisted_source_details(
            record.source_baseline
        )
        snapshot = build_config_snapshot(
            restored,
            source_paths=source_paths,
            source_details=source_details,
            schema_path=self._resolve_schema_path(),
        )
        expected_digest = record.effective_digest
        if snapshot.revision != expected_digest:
            raise ConfigConflictError(
                "持久化配置 digest 校验失败: "
                f"source={loaded_source}, expected={expected_digest}, actual={snapshot.revision}"
            )
        if (
            isinstance(record, ConfigPendingCandidateRecord)
            and record.candidate_digest != snapshot.revision
        ):
            raise ConfigConflictError("pending candidate digest 与 payload 不匹配")
        self._loaded_source = loaded_source
        return snapshot

    @staticmethod
    def _persisted_source_details(
        baseline: dict[str, object],
    ) -> tuple[tuple[ConfigSource, ...], tuple[Path, ...]]:
        details: list[ConfigSource] = []
        for source_key, raw_value in sorted(baseline.items()):
            if not isinstance(raw_value, dict):
                raise TypeError(f"source baseline 必须是对象: key={source_key}")
            raw_vrn = raw_value.get("vrn")
            raw_presence = raw_value.get("presence")
            if raw_vrn is not None and (
                not isinstance(raw_vrn, str) or not raw_vrn
            ):
                raise ValueError(f"source baseline vrn 必须是非空字符串或 None: key={source_key}")
            if raw_presence not in {"present", "absent"}:
                raise ValueError(f"source baseline presence 无效: key={source_key}")
            # source_key=None 的层（inline）在 `_source_baseline` 里退化成
            # `f"{layer}:{precedence}"`，故这里把权威表反查成 `"{layer}:{precedence}"`
            # 一并匹配，保证同一份权威表能逐字还原真层。**禁止兜底**：未登记的持久化键
            # 一律 fail-closed（见 `_source_baseline`），绝不静默映射成 `sqlite` 一类。
            layer, precedence = ConfigSnapshotMixin._resolve_persisted_layer(source_key)
            details.append(
                ConfigSource(
                    vrn=raw_vrn,
                    layer=layer,
                    precedence=precedence,
                    loaded=raw_presence == "present",
                    source_key=source_key,
                    presence=raw_presence,
                    layer_revision=(
                        int(raw_value["layer_revision"])
                        if raw_value.get("layer_revision") is not None
                        else None
                    ),
                    layer_digest=(
                        str(raw_value["layer_digest"])
                        if raw_value.get("layer_digest") is not None
                        else None
                    ),
                    source_generation=(
                        int(raw_value["source_generation"])
                        if raw_value.get("source_generation") is not None
                        else None
                    ),
                )
            )
        # real path 不再持久化，故恢复路径时无 source_paths 可作为监听候选；
        # 监听候选取自显式配置路径（见 start_watching）。
        return tuple(details), ()

    @staticmethod
    def _resolve_persisted_layer(source_key: str) -> tuple[ConfigSourceLayer, int]:
        """把持久化基线键还原为权威层的逻辑来源层与 precedence。

        先按 source_key 直查；再按 `"{layer}:{precedence}"` 退化形态反查（inline）。
        两者都未命中即抛错，不做有损兜底。
        """
        if source_key in _SOURCE_LAYER_AUTHORITY:
            return _SOURCE_LAYER_AUTHORITY[source_key]
        for authority_layer, authority_precedence in _SOURCE_LAYER_AUTHORITY.values():
            if source_key == f"{authority_layer}:{authority_precedence}":
                return authority_layer, authority_precedence
        raise ValueError(
            "source baseline 含未登记的持久化键，无法还原逻辑来源层: "
            f"key={source_key!r}"
        )

    def get_loaded_config_proof(self) -> dict[str, object]:
        """返回可供 Gateway 校验的加载证明，不包含秘密或候选完整 payload。"""

        snapshot = self._require_snapshot()
        active = (
            self._workspace_state_store.get_active_config_snapshot(self._CONFIG_DOMAIN)
            if self._workspace_state_store is not None
            else None
        )
        pending = (
            self._workspace_state_store.get_pending_config_candidate(
                config_domain=self._CONFIG_DOMAIN
            )
            if self._workspace_state_store is not None
            else None
        )
        loaded_pending = (
            pending
            if self._loaded_source == "pending"
            and pending is not None
            and pending.candidate_ref
            else None
        )
        secret_bindings = build_secret_binding_summary(
            snapshot.to_dict(),
            resolve_environment=True,
        )
        secret_binding_digest = hashlib.sha256(
            json.dumps(
                secret_bindings,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        fencing_token = (
            loaded_pending.fencing_token if loaded_pending is not None else None
        )
        return {
            "config_domain": self._CONFIG_DOMAIN,
            "loaded_source": "pending" if loaded_pending is not None else "active",
            "candidate_id": (
                loaded_pending.candidate_id if loaded_pending is not None else None
            ),
            "loaded_commit_revision": (
                loaded_pending.pending_revision
                if loaded_pending is not None
                else active.active_revision if active is not None else None
            ),
            "effective_digest": snapshot.revision,
            "candidate_digest": (
                loaded_pending.candidate_digest if loaded_pending is not None else None
            ),
            "secret_binding_digest": secret_binding_digest,
            "generation_id": self._runtime_generation,
            "fencing_token_digest": (
                hashlib.sha256(fencing_token.encode("utf-8")).hexdigest()
                if fencing_token
                else None
            ),
        }

    def get_pending_startup_contract(
        self,
        *,
        candidate_ref: str,
    ) -> dict[str, object]:
        """只返回新 Workspace generation 所需的 pending 绑定元数据。"""

        return self._pending_restart.get_pending_startup_contract(
            candidate_ref=candidate_ref
        )

    def record_pending_restart_failure(
        self,
        *,
        candidate_ref: str,
        error: str,
        old_runtime_recovered: bool = True,
    ) -> ConfigReloadStatus:
        """记录重启失败，并根据旧 generation 是否恢复选择恢复状态。"""

        return self._pending_restart.record_pending_restart_failure(
            candidate_ref=candidate_ref,
            error=error,
            old_runtime_recovered=old_runtime_recovered,
        )

    def retry_pending_restart(self, *, candidate_ref: str) -> ConfigReloadStatus:
        """为 Workspace pending candidate 创建新的受控重启 generation。"""

        return self._pending_restart.retry_pending_restart(candidate_ref=candidate_ref)

    def resolve_pending_restart(
        self,
        *,
        candidate_ref: str,
        health_proof: dict[str, object],
    ) -> ConfigReloadStatus:
        """用新 generation 的完整 proof 提升已成功启动的 Workspace candidate。"""

        return self._pending_restart.resolve_pending_restart(
            candidate_ref=candidate_ref,
            health_proof=health_proof,
        )

    def discard_pending_restart(
        self,
        *,
        candidate_ref: str,
        expected_active_revision: int,
        expected_active_digest: str,
    ) -> ConfigReloadStatus:
        """仅在旧 active/source 基线可证明安全时丢弃 pending。"""

        return self._pending_restart.discard_pending_restart(
            candidate_ref=candidate_ref,
            expected_active_revision=expected_active_revision,
            expected_active_digest=expected_active_digest,
        )

    @contextmanager
    def use_snapshot(self, snapshot: ConfigSnapshot) -> Iterator[None]:
        token = self._pinned_snapshot.set(snapshot)
        try:
            yield
        finally:
            self._pinned_snapshot.reset(token)

    def get_snapshot(self) -> ConfigSnapshot:
        return self._require_snapshot()

    def get_revision(self) -> str:
        return self._require_snapshot().revision

    def get_reload_status(self) -> ConfigReloadStatus:
        return self._reload_status.get_reload_status()

    def get_source_details(self) -> tuple[ConfigSource, ...]:
        return self._require_snapshot().source_details

    def get_source_diagnostics(
        self,
    ) -> tuple[str, Path, tuple[ConfigSource, ...]]:
        """只读返回配置来源诊断，不初始化快照或迁移 source layer。"""

        if self._snapshot_store.has_snapshot():
            snapshot = self._snapshot_store.current()
            return (
                snapshot.revision,
                snapshot.schema_path or self._resolve_schema_path(),
                snapshot.source_details,
            )
        if self._workspace_state_store is not None:
            active = self._workspace_state_store.get_active_config_snapshot(
                self._CONFIG_DOMAIN
            )
            if active is not None:
                source_details, _ = self._persisted_source_details(
                    active.source_baseline
                )
                return (
                    active.effective_digest,
                    self._resolve_schema_path(),
                    source_details,
                )

        source_details: list[ConfigSource] = [
            ConfigSource(
                vrn=self._inline_source_vrn(),
                layer="inline",
                precedence=0,
                loaded=True,
            ),
            self._config_source(
                path=self._get_workspace_config_path(),
                config_key="workspace_mutable_override",
            ),
            self._config_source(
                path=self._get_workspace_local_config_path(),
                config_key="workspace_local_mutable_override",
            ),
        ]
        if self._workspace_root is not None:
            source_details.append(
                self._config_source(
                    path=get_workspace_config_path(self._workspace_root),
                    config_key="workspace_root_mutable_override",
                )
            )
        if self._workspace_state_store is not None or self._runtime_config_overrides:
            source_details.append(self._runtime_override_source())
        return (
            "uninitialized",
            self._resolve_schema_path(),
            tuple(source_details),
        )

    def get_runtime_override_keys(self) -> tuple[str, ...]:
        return tuple(sorted(self._runtime_config_overrides))

    def config_from_snapshot(self, snapshot: ConfigSnapshot) -> dict[str, Any]:
        return snapshot.to_dict()


__all__ = ["ConfigSnapshotMixin"]
