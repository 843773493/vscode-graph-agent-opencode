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
from app.core.workspace_identity import read_workspace_id
from app.schemas.internal_v2.config import (
    ConfigSourceDocumentDTO,
    ConfigSourceEditDTO,
    ConfigSourceEditRequest,
    ConfigSourceFanoutDTO,
    ConfigSourceFanoutWorkspaceDTO,
    ConfigSourceLayerViewDTO,
    ConfigSourceOriginDTO,
    ConfigSourcePreflightDTO,
)
from app.schemas.internal_v2.model_reference import ModelReferenceDTO
from app.services.infrastructure.config import (
    ConfigRestartRequiredError,
    ConfigSnapshot,
)
from app.services.infrastructure.config.source_document import (
    inspect_config_nodes,
    parse_source_text,
    redact_source_payload,
    redact_source_text,
    restore_source_text,
)
from app.services.infrastructure.config.state import (
    ConfigConflictError,
    ConfigPendingCandidateRecord,
    changed_json_paths,
    prepare_config_for_persistence,
)
from app.services.infrastructure.config.workspace_plugins import (
    workspace_plugin_bindings_for_root,
)
from configs.inheritance.values import json_pointer, thaw
from configs.plugin_parameters import plugin_parameters

if TYPE_CHECKING:
    from app.services.infrastructure.config_service import ConfigService
    from app.services.infrastructure.workspace_state_store import WorkspaceStateStore

_WORKSPACE_SOURCE = "workspace_root_mutable_override"


class ConfigSourceEditMixin:
    def _source_edit_waiting_for_explicit_action(self) -> bool:
        service = cast("ConfigService", self)
        store = service._workspace_state_store
        if store is None:
            return False
        candidate = store.get_pending_config_candidate(config_domain=service._CONFIG_DOMAIN)
        if (candidate is None or candidate.source_edit is None
                or candidate.state in {"candidate_validated", "active", "discarded"}):
            return False
        source_key = candidate.source_edit.get("source_key")
        if not isinstance(source_key, str) or not self._source_writable(source_key):
            return False
        for key, detail in candidate.source_baseline.items():
            if not isinstance(detail, dict):
                return False
            if detail["layer"] == "inline":
                continue
            path, _, _ = self._source_target(key)
            if path is None:
                continue
            file = read_stable_config_file(path)
            if (file.presence, file.digest) != (detail.get("presence"), detail.get("layer_digest")):
                return False
            if key == source_key and file.digest != candidate.source_edit.get("file_digest"):
                return False
        return True

    def _source_fanout(self, source_key: str, generation: int | None = None) -> ConfigSourceFanoutDTO | None:
        service = cast("ConfigService", self)
        owner = service._source_owner
        shared_key = service._shared_source_key(source_key)
        if shared_key is None or owner is None:
            return None
        generation = generation if generation is not None else owner.high_water_mark(source_key=shared_key)
        if generation == 0:
            return None
        records = owner.list_fanout(source_generation=generation, source_key=shared_key)
        workspaces = [ConfigSourceFanoutWorkspaceDTO(workspace_id=record.workspace_id,
            status=record.status, layer_revision=record.layer_revision, layer_digest=record.layer_digest,
            result=record.result, error=record.error) for record in records]
        return ConfigSourceFanoutDTO(source_generation=generation,
            fanout_id=owner.get(source_key=shared_key, source_generation=generation).fanout_id,
            result=("unobserved" if not records else "applied" if all(record.status == "applied" for record in records)
                    else "fanout_partial"), workspaces=workspaces)

    def _saved_source_edit_candidate(self, snapshot: ConfigSnapshot) -> ConfigPendingCandidateRecord | None:
        """watcher 只认领原文件中已有、且完整基线仍吻合的来源意图。"""
        service = cast("ConfigService", self)
        root, store = service._workspace_root, service._workspace_state_store
        if root is None or store is None:
            return None
        pending = store.list_unfinished_source_edit_candidates(config_domain=service._CONFIG_DOMAIN, candidate_digest=snapshot.revision)
        if not pending:
            return None
        active = store.get_active_config_snapshot(service._CONFIG_DOMAIN)
        baseline, _, _, _ = service._source_baseline(snapshot)
        if active is None:
            return None
        payload = prepare_config_for_persistence(snapshot.to_dict())
        for candidate in pending:
            intent = candidate.source_edit
            if intent is None or not self._source_writable(str(intent.get("source_key"))):
                continue
            source_key = str(intent["source_key"])
            path, _, _ = self._source_target(source_key)
            assert path is not None
            file = read_stable_config_file(path)
            selected = baseline.get(source_key)
            if not isinstance(selected, dict) or file.digest != intent["file_digest"]:
                continue
            base_revision = intent["base_layer_revision"]
            target_revision = (int(base_revision) if isinstance(base_revision, int) else 0) + (intent["base_layer_digest"] != intent["file_digest"])
            if (active.active_revision, active.effective_digest) != (intent["expected_active_revision"], intent["expected_active_digest"]):
                continue
            if (selected.get("layer_revision"), selected.get("layer_digest")) != (target_revision, intent["file_digest"]):
                continue
            if candidate.payload == payload and all(
                key == source_key or baseline.get(key) == value for key, value in candidate.source_baseline.items()
            ):
                verify_stable_config_file(file)
                service._validate_agent_definition_references(snapshot)
                return candidate
        return None

    def _source_target(self, source_key: str) -> tuple[Path | None, Path, WorkspaceStateStore]:
        service = cast("ConfigService", self)
        root, store = service._workspace_root, service._workspace_state_store
        if root is None or store is None or store.workspace_root != root:
            raise ValueError("来源编辑需要已绑定精确工作区的配置 owner")
        paths = {key: path for path, (key, _) in service._source_locations().items()}
        graphs = [service._require_snapshot().graph] if service._snapshot_store.has_snapshot() else []
        candidate = getattr(service, "_candidate_source_graph", None)
        if candidate is not None:
            graphs.append(candidate)
        for graph in graphs:
            paths.update({graph.source_keys[uri]: path for uri, path in graph.source_locations.items()})
        paths[service._RUNTIME_OVERRIDE_CONFIG_KEY] = None
        if source_key not in paths:
            raise ValueError("未知配置来源")
        return paths[source_key], root, store

    def _source_writable(self, source_key: str) -> bool:
        service = cast("ConfigService", self)
        return source_key not in {"inline", service._RUNTIME_OVERRIDE_CONFIG_KEY} and any(
            detail.source_key == source_key for detail in service._require_snapshot().source_details)

    def get_source_document(
        self, *, source_key: str, gateway_id: str, idempotency_key: str | None = None,
    ) -> ConfigSourceDocumentDTO:
        service = cast("ConfigService", self)
        path, root, store = self._source_target(source_key)
        if not service._snapshot_store.has_snapshot():
            raise RuntimeError("来源读取要求工作区已完成初始化")
        workspace_id = read_workspace_id(root)
        source_effective, source_payloads, files, baseline = service._read_saved_source_view()
        file = files.get(source_key)
        text = (file.raw_bytes.decode("utf-8") if file is not None and file.raw_bytes is not None
                else json.dumps({"ui": service._runtime_config_overrides}) if path is None else "{}\n")
        record = store.get_source_layer(source_key)
        active = store.get_active_config_snapshot(service._CONFIG_DOMAIN)
        edit = None
        fanout_generation = None
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
                selected = pending.source_baseline.get(source_key)
                if intent.get("stage") == "imported" and isinstance(selected, dict):
                    generation = selected.get("source_generation")
                    if isinstance(generation, int):
                        fanout_generation = generation
        graph = service._candidate_source_graph
        paths = {graph.source_keys[uri]: path for uri, path in graph.source_locations.items()}
        details = {key: (service._source_identity(path)[1], index)
                   for index, key in enumerate(source_payloads) if key in paths
                   for path in [paths[key]]}
        details[service._RUNTIME_OVERRIDE_CONFIG_KEY] = ("runtime_override", len(details))
        if source_key not in details:
            raise ConfigConflictError("配置来源已不在当前入口实际依赖中")
        layer, _ = details[source_key]
        selected = next((node for node in graph.resolve_scope().chain
                         if node.location.path == path and not node.location.pointer), None)
        inherited = thaw(selected.parent.settings) if selected is not None and selected.parent is not None else {}
        inherited_payload = {**inherited.get("workspace", {}),
            **({"network": inherited["network"]} if "network" in inherited else {})}
        inherited_origins = []
        if selected is not None and selected.parent is not None:
            workspace = cast(dict[str, object], inherited.get("workspace", {}))
            llm = cast(dict[str, object], workspace.get("llm", {}))
            for prefix, definitions in (
                (("agents",), cast(dict[str, object], workspace.get("agents", {}))),
                (("llm", "providers"), cast(dict[str, object], llm.get("providers", {}))),
            ):
                for identity in definitions:
                    path = (*prefix, identity)
                    origin = selected.parent.origins.locate(("workspace", *path))
                    inherited_origins.append(ConfigSourceOriginDTO(
                        path=json_pointer(path), source_key=graph.source_keys[origin.source_uri], shadowed_sources=[],
                    ))
        return ConfigSourceDocumentDTO(
            gateway_id=gateway_id, workspace_id=workspace_id, source_key=source_key,
            layer=layer, writable=self._source_writable(source_key),
            jsonc=redact_source_text(text, payload=source_payloads[source_key] if file is not None else None),
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
            source_inherited=cast(dict[str, object], redact_source_payload(inherited_payload)),
            source_inherited_origins=inherited_origins,
            nodes=inspect_config_nodes(graph, graph.nodes),
            layers=[ConfigSourceLayerViewDTO(
                source_key=key, layer=details[key][0], precedence=details[key][1],
                writable=self._source_writable(key),
                payload=cast(dict[str, object], redact_source_payload(payload)),
            ) for key, payload in source_payloads.items()],
            origins=[ConfigSourceOriginDTO.model_validate(origin) for origin in service._graph_origins(graph)],
            active_origins=[ConfigSourceOriginDTO.model_validate(origin) for origin in service._require_snapshot().source_origins()],
            source_baseline=baseline,
            status=service.get_reload_status_dto(), edit=edit, fanout=self._source_fanout(source_key, fanout_generation),
        )

    def _check_source_edit(
        self, source_key: str, gateway_id: str, request: ConfigSourceEditRequest,
        accepted: ConfigPendingCandidateRecord | None = None,
    ) -> StableConfigFile:
        service = cast("ConfigService", self)
        path, root, store = self._source_target(source_key)
        if not self._source_writable(source_key) or path is None:
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
        # 已接受的同一请求在 replace 后恢复时，原文件就是已保存正文；不再用旧下标重放草稿。
        text = (original if accepted is not None and accepted.source_edit is not None
                and file.digest == accepted.source_edit["file_digest"] else restore_source_text(
                    request.jsonc, original,
                ))
        candidate = service._build_candidate_snapshot(source_override=(source_key, parse_source_text(text)),
            import_sources=False)
        if not any(source.source_key == source_key for source in candidate.source_details):
            raise ConfigConflictError("编辑后来源已不在实际父链中，拒绝写入退役来源")
        service._validate_agent_definition_references(candidate)
        verify_stable_config_file(file)
        return text, candidate

    def _build_probe_draft_snapshot(self, *, source_key: str, jsonc: str, expected_source_baseline) -> ConfigSnapshot:
        service = cast("ConfigService", self)
        _, _, files, baseline = service._read_saved_source_view()
        if expected_source_baseline != baseline:
            raise ConfigConflictError("参与来源的文件或版本已改变，请重新读取原来源后显式重试")
        if source_key not in files:
            raise ValueError("草稿探测必须选择实际参与的文件来源")
        file = files[source_key]
        original = file.raw_bytes.decode("utf-8") if file.raw_bytes is not None else "{}\n"
        frozen = {item.path: item for item in files.values()}
        snapshot = service._build_candidate_snapshot(import_sources=False,
            source_override=(source_key, parse_source_text(restore_source_text(jsonc, original))), frozen_files=frozen)
        for dependency in snapshot.graph.dependencies.values():
            verify_stable_config_file(dependency)
        return snapshot

    async def preflight_source_edit(
        self, *, source_key: str, gateway_id: str, request: ConfigSourceEditRequest,
        model_ref: ModelReferenceDTO | None = None, provider_id: str | None = None,
    ) -> ConfigSourcePreflightDTO:
        service = cast("ConfigService", self)
        _, candidate = self._preflight_source_edit(source_key, gateway_id, request)
        await service._validate_gateway_workspace_plugin_bindings(
            previous=service._require_snapshot(), candidate=candidate, candidate_id=request.idempotency_key,
            gateway_id=gateway_id,
        )
        with service._mcp_configuration_guard(service._require_snapshot().to_dict(), candidate.to_dict()):
            pass
        active = service._require_snapshot()
        active_config = active.to_dict()
        if provider_id is None and model_ref is not None:
            provider_id = model_ref.provider_id
        active_models = (active_config["llm"]["providers"].get(model_ref.provider_id, {}).get("models", {})
                         if model_ref is not None else {})
        return ConfigSourcePreflightDTO(
            document=self.get_source_document(source_key=source_key, gateway_id=gateway_id),
            candidate=cast(dict[str, object], redact_source_payload(candidate.to_dict())),
            candidate_digest=candidate.revision,
            changed_paths=list(changed_json_paths(thaw(service._require_snapshot().effective_values), thaw(candidate.effective_values))),
            model_options=service._llm_resolution.inspect_model_reference(candidate.graph, model_ref)
                if model_ref is not None else None,
            active_model_options=service._llm_resolution.inspect_model_reference(active.graph, model_ref)
                if model_ref is not None and model_ref.model_id in active_models else None,
            provider_options=service._llm_resolution.inspect_provider(candidate.graph, provider_id)
                if provider_id is not None else None,
            active_provider_options=service._llm_resolution.inspect_provider(active.graph, provider_id)
                if provider_id is not None and provider_id in active_config["llm"]["providers"] else None,
        )

    async def save_source_edit(
        self, *, source_key: str, gateway_id: str, request: ConfigSourceEditRequest,
        service_plugin_runtime_manage: bool = True,
    ) -> ConfigSourceDocumentDTO:
        service = cast("ConfigService", self)
        path, _, store = self._source_target(source_key)
        if not self._source_writable(source_key) or path is None:
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
        previous_file = read_stable_config_file(path)
        previous_source = parse_source_text(
            previous_file.raw_bytes.decode("utf-8") if previous_file.raw_bytes is not None else "{}\n"
        )
        text, candidate = self._preflight_source_edit(source_key, gateway_id, request, pending)
        verify_stable_config_file(previous_file)

        def require_plugin_permission() -> None:
            current = service._require_snapshot().to_dict()
            desired = candidate.to_dict()
            if not service_plugin_runtime_manage and (
                workspace_plugin_bindings_for_root(current, workspace_root=service._workspace_root)
                != workspace_plugin_bindings_for_root(desired, workspace_root=service._workspace_root)
                or plugin_parameters(current) != plugin_parameters(desired)
            ):
                raise PermissionError("当前 Gateway 未授权修改 Workspace 插件运行绑定")

        require_plugin_permission()
        # 原校验入口按绑定／参数变化判断；空参数也不能绕过写文件前的版本准入。
        previous = service._require_snapshot()
        await service._validate_gateway_workspace_plugin_bindings(
            previous=previous, candidate=candidate, candidate_id=request.idempotency_key,
            gateway_id=gateway_id,
        )
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
                require_plugin_permission()
                service._validate_agent_definition_references(candidate)
                file = self._check_source_edit(source_key, gateway_id, request, accepted)
                if accepted is None:
                    service._prepare_candidate(service._require_snapshot(), candidate, idempotency_key=request.idempotency_key, source_edit=intent)
                verify_stable_config_file(file)
                if file.digest != intent["file_digest"]:
                    atomic_write_bytes(file.path, text.encode("utf-8"))
                shared_key = service._shared_source_key(source_key)
                if shared_key is not None and service._source_owner is not None:
                    service._source_owner.observe(uri=service._source_uri_for_path(file.path), presence="present",
                        layer_digest=str(intent["file_digest"]), origin="source-writer", source_key=shared_key)
                service._remove_released_plugin_private_inputs(previous_source)
                return True

        try:
            with service._mcp_configuration_guard(previous.to_dict(), candidate.to_dict()):
                if service._mcp_plugin_preparation is not None:
                    self._check_source_edit(source_key, gateway_id, request, pending)
                    service._cancel_disabled_plugin_preparation(candidate)
                await service.reload(candidate_applier=service._candidate_applier, idempotency_key=request.idempotency_key,
                                  before_build=save, source_edit=intent)
        except ConfigRestartRequiredError:
            # 来源保存已完成；pending/restart 仍由原状态机持有，不伪造 active。
            pass
        return self.get_source_document(source_key=source_key, gateway_id=gateway_id, idempotency_key=request.idempotency_key)

    async def retry_source_edit(
        self, *, source_key: str, gateway_id: str, idempotency_key: str,
    ) -> ConfigSourceDocumentDTO:
        """显式重试原来源候选；不改写文件，也不重放结果未知的副作用。"""
        service = cast("ConfigService", self)
        _, root, store = self._source_target(source_key)
        pending = store.get_pending_config_candidate(config_domain=service._CONFIG_DOMAIN, idempotency_key=idempotency_key)
        if pending is None or pending.source_edit is None or pending.state not in {"rejected", "candidate_validated"}:
            raise ConfigConflictError("仅可显式重试原来源的未应用候选")
        intent = pending.source_edit
        if (intent.get("gateway_id"), intent.get("workspace_id"), intent.get("source_key")) != (gateway_id, read_workspace_id(root), source_key):
            raise ConfigConflictError("来源重试不属于请求的 Gateway／工作区／来源")

        def retry() -> bool:
            if store.get_config_apply_claim(config_domain=service._CONFIG_DOMAIN) is not None:
                raise ConfigConflictError("已有配置 apply claim，不能重试来源候选")
            snapshot = service._build_candidate_snapshot(import_sources=False)
            saved = self._saved_source_edit_candidate(snapshot)
            if saved is None or saved.candidate_id != pending.candidate_id:
                raise ConfigConflictError("原来源候选的文件、层或 active 基线已改变，不能重试")
            if saved.state == "rejected":
                store.update_pending_config_candidate_state(config_domain=service._CONFIG_DOMAIN,
                    candidate_id=saved.candidate_id, expected_state="rejected", state="candidate_validated", last_error=None)
            elif saved.state != "candidate_validated":
                raise ConfigConflictError("原来源候选状态已改变，不能重试")
            return True

        await service.reload(candidate_applier=service._candidate_applier, idempotency_key=idempotency_key,
            before_build=retry, source_edit=intent)
        return self.get_source_document(source_key=source_key, gateway_id=gateway_id, idempotency_key=idempotency_key)
