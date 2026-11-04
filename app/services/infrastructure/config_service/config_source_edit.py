"""精确 workspace 来源的观测、无副作用预检与原候选链保存。"""

from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path
from typing import TYPE_CHECKING, cast

import portalocker

from app.core.atomic_fs import atomic_write_bytes
from app.core.config_sources import (
    StableConfigFile,
    config_revision,
    read_stable_config_file,
    verify_stable_config_file,
)
from app.core.path_utils import get_workspace_config_path
from app.core.workspace_identity import read_workspace_id
from app.schemas.internal_v2.config import (
    ConfigSourceDocumentDTO,
    ConfigSourceEditDTO,
    ConfigSourceEditRequest,
    ConfigSourceLayerViewDTO,
    ConfigSourceOriginDTO,
    ConfigSourcePreflightDTO,
)
from app.services.infrastructure.config import (
    ConfigRestartRequiredError,
    ConfigSnapshot,
)
from app.services.infrastructure.config.source_document import (
    parse_source_text,
    redact_source_payload,
    redact_source_text,
    restore_source_text,
    source_origins,
)
from app.services.infrastructure.config.state import (
    ConfigConflictError,
    ConfigPendingCandidateRecord,
    changed_json_paths,
    prepare_config_for_persistence,
)
from app.services.infrastructure.config_service.config_service_common import (
    _SOURCE_LAYER_AUTHORITY,
)

if TYPE_CHECKING:
    from app.services.infrastructure.config_service import ConfigService
    from app.services.infrastructure.workspace_state_store import WorkspaceStateStore

_WORKSPACE_SOURCE = "workspace_root_mutable_override"


class ConfigSourceEditMixin:
    def _saved_source_edit_candidate(self, snapshot: ConfigSnapshot) -> ConfigPendingCandidateRecord | None:
        """watcher 只认领原文件中已有、且完整基线仍吻合的来源意图。"""
        service = cast("ConfigService", self)
        root, store = service._workspace_root, service._workspace_state_store
        if root is None or store is None:
            return None
        pending = store.list_unfinished_source_edit_candidates(config_domain=service._CONFIG_DOMAIN, candidate_digest=snapshot.revision)
        if not pending:
            return None
        file = read_stable_config_file(get_workspace_config_path(root))
        active = store.get_active_config_snapshot(service._CONFIG_DOMAIN)
        baseline, _, _, _ = service._source_baseline(snapshot)
        selected = baseline.get(_WORKSPACE_SOURCE)
        if active is None or not isinstance(selected, dict):
            return None
        payload = prepare_config_for_persistence(snapshot.to_dict())
        for candidate in pending:
            intent = candidate.source_edit
            if intent is None or intent.get("source_key") != _WORKSPACE_SOURCE or file.digest != intent["file_digest"]:
                continue
            base_revision = intent["base_layer_revision"]
            target_revision = (int(base_revision) if isinstance(base_revision, int) else 0) + (intent["base_layer_digest"] != intent["file_digest"])
            if (active.active_revision, active.effective_digest) != (intent["expected_active_revision"], intent["expected_active_digest"]):
                continue
            if (selected.get("layer_revision"), selected.get("layer_digest")) != (target_revision, intent["file_digest"]):
                continue
            if candidate.payload == payload and all(
                key == _WORKSPACE_SOURCE or baseline.get(key) == value for key, value in candidate.source_baseline.items()
            ):
                verify_stable_config_file(file)
                return candidate
        return None

    def _source_target(self, source_key: str) -> tuple[Path | None, Path, WorkspaceStateStore]:
        service = cast("ConfigService", self)
        root, store = service._workspace_root, service._workspace_state_store
        if root is None or store is None or store.workspace_root != root:
            raise ValueError("来源编辑需要已绑定精确工作区的配置 owner")
        if source_key not in _SOURCE_LAYER_AUTHORITY:
            raise ValueError("未知配置来源")
        paths = {
            "inline": service._inline_config_path,
            "workspace_mutable_override": service._get_workspace_config_path(),
            "workspace_local_mutable_override": service._get_workspace_local_config_path(),
            _WORKSPACE_SOURCE: get_workspace_config_path(root),
            service._RUNTIME_OVERRIDE_CONFIG_KEY: None,
        }
        return paths[source_key], root, store

    def get_source_document(
        self, *, source_key: str, gateway_id: str, idempotency_key: str | None = None,
    ) -> ConfigSourceDocumentDTO:
        service = cast("ConfigService", self)
        path, root, store = self._source_target(source_key)
        if not service._snapshot_store.has_snapshot():
            raise RuntimeError("来源读取要求工作区已完成初始化")
        workspace_id = read_workspace_id(root)
        file = read_stable_config_file(path) if path is not None else None
        text = (file.raw_bytes.decode("utf-8") if file is not None and file.raw_bytes is not None
                else json.dumps({"ui": service._runtime_config_overrides}) if path is None else "{}\n")
        source_payloads: dict[str, dict[str, object]] = {}
        source_effective, _, _, _ = service._read_effective_config(import_sources=False, source_payloads=source_payloads)
        record = store.get_source_layer(source_key)
        active = store.get_active_config_snapshot(service._CONFIG_DOMAIN)
        edit = None
        if idempotency_key is not None:
            pending = store.get_pending_config_candidate(
                config_domain=service._CONFIG_DOMAIN, idempotency_key=idempotency_key,
            )
            if pending is not None and pending.source_edit is not None:
                intent = pending.source_edit
                if (intent.get("gateway_id"), intent.get("workspace_id"), intent.get("source_key")) != (gateway_id, workspace_id, source_key):
                    raise ConfigConflictError("来源提交身份不属于请求的 Gateway／工作区／来源")
                edit = ConfigSourceEditDTO(
                    idempotency_key=pending.idempotency_key, candidate_id=pending.candidate_id,
                    state=pending.state, file_digest=str(intent["file_digest"]),
                    source_saved=intent.get("stage") == "imported" or (file is not None and file.digest == intent["file_digest"]),
                    layer_imported=intent.get("stage") == "imported" or (record is not None and record.layer_digest == intent["file_digest"]),
                )
        layer, _ = _SOURCE_LAYER_AUTHORITY[source_key]
        return ConfigSourceDocumentDTO(
            gateway_id=gateway_id, workspace_id=workspace_id, source_key=source_key,
            layer=layer, writable=source_key == _WORKSPACE_SOURCE,
            jsonc=redact_source_text(text),
            file_presence=file.presence if file is not None else "absent",
            file_digest=file.digest if file is not None else None,
            layer_revision=record.layer_revision if record is not None else None,
            layer_digest=record.layer_digest if record is not None else None,
            source_generation=record.source_generation if record is not None else None,
            active_revision=active.active_revision if active is not None else None,
            active_digest=active.effective_digest if active is not None else None,
            json_schema=service._load_schema(),
            effective=cast(dict[str, object], redact_source_payload(service._require_snapshot().to_dict())),
            source_effective=cast(dict[str, object], redact_source_payload(source_effective)),
            layers=[ConfigSourceLayerViewDTO(
                source_key=key, layer=_SOURCE_LAYER_AUTHORITY[key][0], precedence=_SOURCE_LAYER_AUTHORITY[key][1],
                writable=key == _WORKSPACE_SOURCE,
                payload=cast(dict[str, object], redact_source_payload(payload)),
            ) for key, payload in source_payloads.items()],
            origins=[ConfigSourceOriginDTO.model_validate(origin) for origin in source_origins(source_payloads, source_effective)],
            status=service.get_reload_status_dto(), edit=edit,
        )

    def _check_source_edit(
        self, source_key: str, gateway_id: str, request: ConfigSourceEditRequest,
        accepted: ConfigPendingCandidateRecord | None = None,
    ) -> StableConfigFile:
        service = cast("ConfigService", self)
        path, root, store = self._source_target(source_key)
        if source_key != _WORKSPACE_SOURCE or path is None:
            raise ValueError("此来源未提供文件 writer，只允许读取")
        if (request.gateway_id != gateway_id or request.workspace_id != read_workspace_id(root)
                or request.source_key != source_key):
            raise ConfigConflictError("编辑目标与请求的 Gateway／工作区／来源不一致")
        file = read_stable_config_file(path)
        source = store.get_source_layer(source_key)
        active = store.get_active_config_snapshot(service._CONFIG_DOMAIN)
        baseline = (
            file.digest, source.layer_revision if source is not None else None,
            source.layer_digest if source is not None else None,
            active.active_revision if active is not None else None,
            active.effective_digest if active is not None else None,
        )
        expected = (request.base_file_digest, request.base_layer_revision, request.base_layer_digest,
                    request.expected_active_revision, request.expected_active_digest)
        if accepted is not None and accepted.source_edit is not None:
            intent = accepted.source_edit
            target_revision = (request.base_layer_revision or 0) + (request.base_layer_digest != intent["file_digest"])
            allowed_layers = {(request.base_layer_revision, request.base_layer_digest), (target_revision, intent["file_digest"])}
            allowed_files = {request.base_file_digest, intent["file_digest"]}
            if intent.get("stage") == "imported":
                allowed_layers = {(target_revision, intent["file_digest"])}
                allowed_files = {intent["file_digest"]}
            if baseline[0] in allowed_files and baseline[1:3] in allowed_layers and baseline[3:] == expected[3:]:
                return file
        elif baseline == expected:
            return file
        raise ConfigConflictError("来源文件、层或 active 的 CAS 基线已改变，请重新读取原目标")

    def _preflight_source_edit(
        self, source_key: str, gateway_id: str, request: ConfigSourceEditRequest,
        accepted: ConfigPendingCandidateRecord | None = None,
    ) -> tuple[str, ConfigSnapshot]:
        service = cast("ConfigService", self)
        file = self._check_source_edit(source_key, gateway_id, request, accepted)
        original = file.raw_bytes.decode("utf-8") if file.raw_bytes is not None else "{}\n"
        text = restore_source_text(request.jsonc, original)
        candidate = service._build_candidate_snapshot(workspace_source=parse_source_text(text), import_sources=False)
        verify_stable_config_file(file)
        return text, candidate

    def preflight_source_edit(
        self, *, source_key: str, gateway_id: str, request: ConfigSourceEditRequest,
    ) -> ConfigSourcePreflightDTO:
        service = cast("ConfigService", self)
        _, candidate = self._preflight_source_edit(source_key, gateway_id, request)
        return ConfigSourcePreflightDTO(
            document=self.get_source_document(source_key=source_key, gateway_id=gateway_id),
            candidate=cast(dict[str, object], redact_source_payload(candidate.to_dict())),
            candidate_digest=candidate.revision,
            changed_paths=list(changed_json_paths(service._require_snapshot().to_dict(), candidate.to_dict())),
        )

    async def save_source_edit(
        self, *, source_key: str, gateway_id: str, request: ConfigSourceEditRequest,
    ) -> ConfigSourceDocumentDTO:
        service = cast("ConfigService", self)
        path, _, store = self._source_target(source_key)
        if source_key != _WORKSPACE_SOURCE or path is None:
            raise ValueError("此来源未提供文件 writer，只允许读取")
        request_digest = config_revision(request.model_dump())
        pending = store.get_pending_config_candidate(
            config_domain=service._CONFIG_DOMAIN, idempotency_key=request.idempotency_key,
        )
        if pending is not None:
            if pending.source_edit is None or pending.source_edit.get("request_digest") != request_digest:
                raise ConfigConflictError("来源编辑幂等键已绑定其它请求")
            if pending.state != "candidate_validated" or store.get_config_apply_claim(config_domain=service._CONFIG_DOMAIN) is not None:
                return self.get_source_document(source_key=source_key, gateway_id=gateway_id, idempotency_key=request.idempotency_key)
        text, candidate = self._preflight_source_edit(source_key, gateway_id, request, pending)
        intent: dict[str, object] = {"gateway_id": gateway_id, "workspace_id": request.workspace_id,
                  "source_key": source_key, "request_digest": request_digest, "file_digest": sha256(text.encode("utf-8")).hexdigest(),
                  "stage": "accepted", "base_file_digest": request.base_file_digest,
                  "base_layer_revision": request.base_layer_revision, "base_layer_digest": request.base_layer_digest,
                  "expected_active_revision": request.expected_active_revision, "expected_active_digest": request.expected_active_digest}

        def save() -> bool:
            # 锁只协调同一来源 writer；原生编辑仍通过紧邻 replace 的文件快照 CAS 拒绝。
            with portalocker.Lock(str(path) + ".lock", mode="a", flags=portalocker.LOCK_EX | portalocker.LOCK_NB):
                accepted = store.get_pending_config_candidate(config_domain=service._CONFIG_DOMAIN, idempotency_key=request.idempotency_key)
                claim = store.get_config_apply_claim(config_domain=service._CONFIG_DOMAIN)
                if accepted is not None:
                    if accepted.source_edit is None or accepted.source_edit.get("request_digest") != request_digest:
                        raise ConfigConflictError("来源编辑幂等键已绑定其它请求")
                    if accepted.state != "candidate_validated" or claim is not None:
                        return False
                elif claim is not None:
                    raise ConfigConflictError("已有配置 apply claim，禁止接受另一来源 intent")
                file = self._check_source_edit(source_key, gateway_id, request, accepted)
                if accepted is None:
                    service._prepare_candidate(service._require_snapshot(), candidate, idempotency_key=request.idempotency_key, source_edit=intent)
                verify_stable_config_file(file)
                if file.digest != intent["file_digest"]:
                    atomic_write_bytes(file.path, text.encode("utf-8"))
                return True

        try:
            await service.reload(candidate_applier=service._candidate_applier, idempotency_key=request.idempotency_key,
                              before_build=save, source_edit=intent)
        except ConfigRestartRequiredError:
            # 来源保存已完成；pending/restart 仍由原状态机持有，不伪造 active。
            pass
        return self.get_source_document(source_key=source_key, gateway_id=gateway_id, idempotency_key=request.idempotency_key)
