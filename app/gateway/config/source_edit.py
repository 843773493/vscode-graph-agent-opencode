"""Gateway 来源正文、纯预检及原 pending/claim 链上的文件保存。"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import asdict
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
from app.schemas.gateway.config import (
    GatewayConfigReloadStatusDTO,
    GatewayConfigSourceDocumentDTO,
    GatewayConfigSourceEditDTO,
    GatewayConfigSourceEditRequest,
    GatewayConfigSourceLayerViewDTO,
    GatewayConfigSourceOriginDTO,
    GatewayConfigSourcePreflightDTO,
)
from app.services.infrastructure.config.plugin_private_inputs import (
    private_input_references,
)
from app.services.infrastructure.config.policy import gateway_config_policy
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
from configs.inheritance.values import ConfigValue, json_pointer, thaw
from configs.runtime import read_jsonc_object

from .loader import load_gateway_config
from .values import GatewayConfig

if TYPE_CHECKING:
    from . import GatewayConfigReloadService


class GatewaySourceEditMixin:
    def _source_path(self, source_key: str) -> Path:
        service = cast("GatewayConfigReloadService", self)
        if service._gateway_id is None:
            raise ConfigConflictError("Gateway 来源编辑需要已绑定原生 Gateway 身份的 owner")
        paths = {
            "inline:0": service.config.inline_path,
            "gateway_mutable_override": service._config_path,
            "gateway_local_mutable_override": service._local_config_path,
        }
        if service.config.graph is not None:
            paths.update({source.source_key: service.config.graph.source_locations[source.uri]
                          for source in service.config.source_details
                          if source.source_key is not None and source.uri in service.config.graph.source_locations})
        if source_key not in paths:
            raise ValueError("未知 Gateway 配置来源")
        path = paths[source_key]
        if path is None:
            raise RuntimeError("Gateway 来源缺少实际文件定位")
        return path

    def _source_config(
        self, *, overrides: dict[str, dict[str, object]] | None = None,
        payloads: dict[str, dict[str, object]] | None = None,
    ) -> GatewayConfig:
        service = cast("GatewayConfigReloadService", self)
        return load_gateway_config(
            config_path=service._config_path, local_config_path=service._local_config_path,
            inline_config_path=service.config.inline_path, schema_path=service._schema_path or service.config.schema_path,
            entry_path=service.config.entry_path, gateway_id=service._gateway_id,
            state_store=service._state_store, persist_migrations=False,
            source_overrides=overrides, source_payloads=payloads, allow_new_connection_ids=False,
        )

    def get_reload_status_dto(self) -> GatewayConfigReloadStatusDTO:
        status = cast("GatewayConfigReloadService", self).status()
        return GatewayConfigReloadStatusDTO.model_validate({
            **asdict(status), "available": True, "layer_digests": status.layer_digests or {},
        })

    def get_source_document(self, *, source_key: str, idempotency_key: str | None = None) -> GatewayConfigSourceDocumentDTO:
        service = cast("GatewayConfigReloadService", self)
        file = read_stable_config_file(self._source_path(source_key))
        text = file.raw_bytes.decode("utf-8") if file.raw_bytes is not None else "{}\n"
        payloads: dict[str, dict[str, object]] = {}
        source_config = self._source_config(payloads=payloads)
        if source_config.schema_path is None or service._gateway_id is None:
            raise RuntimeError("Gateway 来源 owner 缺少 schema 或目标身份")
        schema = read_jsonc_object(source_config.schema_path)
        payloads = {key: {**cast(dict[str, object], payload["settings"]).get("gateway", {}),
                          **({"network": payload["settings"]["network"]} if "network" in payload["settings"] else {})}
                    for key, payload in payloads.items()}
        if source_config.graph is None:
            raise RuntimeError("Gateway 来源缺少已解析节点")
        settings = cast(dict[str, object], thaw(source_config.graph.resolve_scope().settings))
        source_payload = {**cast(dict[str, object], settings["gateway"]),
                          "network": settings["network"], "config_version": 6}
        details = {source.source_key: source for source in source_config.source_details}
        if source_key not in details:
            raise ValueError("Gateway 配置来源不在当前入口的父链中")
        selected = next((node for node in source_config.graph.resolve_scope().chain
                         if node.location.path == file.path and not node.location.pointer), None)
        inherited = (cast(dict[str, object], thaw(selected.parent.settings))
                     if selected is not None and selected.parent is not None else {})
        inherited_payload = {**cast(dict[str, object], inherited.get("gateway", {})),
                             **({"network": inherited["network"]} if "network" in inherited else {})}
        verify_stable_config_file(file)
        store = service._state_store
        record = store.get_source_layer(source_key)
        active = store.get_active_config_snapshot(service._CONFIG_DOMAIN)
        edit = None
        if idempotency_key is not None:
            pending = store.get_pending_config_candidate(config_domain=service._CONFIG_DOMAIN, idempotency_key=idempotency_key)
            if pending is not None and pending.source_edit is not None:
                intent = pending.source_edit
                if (intent.get("gateway_id"), intent.get("source_key")) != (service._gateway_id, source_key):
                    raise ConfigConflictError("来源提交身份不属于当前 Gateway／来源")
                edit = GatewayConfigSourceEditDTO(
                    idempotency_key=pending.idempotency_key, candidate_id=pending.candidate_id,
                    state=pending.state, file_digest=str(intent["file_digest"]),
                    source_saved=intent.get("stage") == "imported" or file.digest == intent["file_digest"],
                    layer_imported=intent.get("stage") == "imported" or (record is not None and record.layer_digest == intent["file_digest"]),
                )
        return GatewayConfigSourceDocumentDTO(
            gateway_id=service._gateway_id, source_key=source_key, layer=details[source_key].layer,
            writable=source_key != "inline:0", jsonc=redact_source_text(text), file_presence=file.presence, file_digest=file.digest,
            layer_revision=record.layer_revision if record else None, layer_digest=record.layer_digest if record else None,
            source_generation=record.source_generation if record else None,
            active_revision=active.active_revision if active else None, active_digest=active.effective_digest if active else None,
            json_schema=schema,
            effective=cast(dict[str, object], redact_source_payload(service.config.payload)),
            source_effective=cast(dict[str, object], redact_source_payload(source_payload)),
            source_inherited=cast(dict[str, object], redact_source_payload(inherited_payload)),
            nodes=inspect_config_nodes(source_config.graph, source_config.graph.nodes),
            layers=[GatewayConfigSourceLayerViewDTO(
                source_key=key, layer=details[key].layer, precedence=details[key].precedence,
                writable=key != "inline:0", payload=cast(dict[str, object], redact_source_payload(payload)),
            ) for key, payload in payloads.items()],
            origins=self._source_origins(source_config),
            status=self.get_reload_status_dto(), edit=edit,
        )

    @staticmethod
    def _source_origins(config: GatewayConfig) -> list[GatewayConfigSourceOriginDTO]:
        """直接投影解析器的真实定义来源，不再次合并配置。"""
        if config.graph is None:
            raise RuntimeError("Gateway 来源缺少已解析节点")
        scope = config.graph.resolve_scope()
        keys = {detail.uri: detail.source_key for detail in config.source_details}
        result = []

        def visit(value: ConfigValue, path: tuple[str, ...]) -> None:
            if isinstance(value, Mapping) and value:
                for key, child in value.items():
                    visit(child, (*path, key))
                return
            origin = scope.origins.locate(path)
            source_key = keys[origin.source_uri]
            sources = []
            for node in scope.chain:
                try:
                    key = keys[node.origins.locate(path).source_uri]
                except KeyError:
                    continue
                if key != source_key and key not in sources:
                    sources.append(key)
            result.append(GatewayConfigSourceOriginDTO(
                path=json_pointer(path[1:] if path[0] == "gateway" else path),
                source_key=source_key, shadowed_sources=sources,
            ))

        for key, value in scope.settings.items():
            visit(value, (key,))
        return result

    def _check_source_edit(
        self, source_key: str, request: GatewayConfigSourceEditRequest, accepted: ConfigPendingCandidateRecord | None = None,
    ) -> StableConfigFile:
        service = cast("GatewayConfigReloadService", self)
        path = self._source_path(source_key)
        if source_key == "inline:0":
            raise ValueError("Gateway inline 来源只读")
        if (request.gateway_id, request.source_key) != (service._gateway_id, source_key):
            raise ConfigConflictError("编辑目标与原生 Gateway／来源不一致")
        if not any(source.source_key == source_key for source in service.config.source_details):
            raise ConfigConflictError("Gateway 配置来源已不在当前入口的父链中，请重新读取原目标")
        file = read_stable_config_file(path)
        source = service._state_store.get_source_layer(source_key)
        active = service._state_store.get_active_config_snapshot(service._CONFIG_DOMAIN)
        baseline = (file.digest, source.layer_revision if source else None, source.layer_digest if source else None,
                    active.active_revision if active else None, active.effective_digest if active else None)
        expected = (request.base_file_digest, request.base_layer_revision, request.base_layer_digest,
                    request.expected_active_revision, request.expected_active_digest)
        if accepted is not None and accepted.source_edit is not None:
            intent = accepted.source_edit
            target_revision = (request.base_layer_revision or 0) + (request.base_layer_digest != intent["file_digest"])
            files = {request.base_file_digest, intent["file_digest"]}
            layers = {(request.base_layer_revision, request.base_layer_digest), (target_revision, intent["file_digest"])}
            if intent.get("stage") == "imported":
                files, layers = {intent["file_digest"]}, {(target_revision, intent["file_digest"])}
            if baseline[0] in files and baseline[1:3] in layers and baseline[3:] == expected[3:]:
                return file
        elif baseline == expected:
            return file
        raise ConfigConflictError("Gateway 来源文件、层或 active 的 CAS 基线已改变，请重新读取原目标")

    def _preflight_source_edit(
        self, source_key: str, request: GatewayConfigSourceEditRequest, accepted: ConfigPendingCandidateRecord | None = None,
    ) -> tuple[str, GatewayConfig]:
        file = self._check_source_edit(source_key, request, accepted)
        original = file.raw_bytes.decode("utf-8") if file.raw_bytes is not None else "{}\n"
        text = restore_source_text(request.jsonc, original)
        candidate = self._source_config(overrides={source_key: parse_source_text(text)})
        if not any(source.source_key == source_key for source in candidate.source_details):
            raise ConfigConflictError("Gateway 配置来源已不在候选入口的父链中，请重新读取原目标")
        verify_stable_config_file(file)
        self._verify_source_dependencies(candidate)
        return text, candidate

    @staticmethod
    def _verify_source_dependencies(candidate: GatewayConfig) -> None:
        if candidate.graph is None:
            raise RuntimeError("Gateway 来源候选缺少父依赖")
        try:
            for dependency in candidate.graph.dependencies.values():
                verify_stable_config_file(dependency)
        except RuntimeError as error:
            raise ConfigConflictError("Gateway 来源候选的父依赖已改变") from error

    def preflight_source_edit(self, *, source_key: str, request: GatewayConfigSourceEditRequest) -> GatewayConfigSourcePreflightDTO:
        service = cast("GatewayConfigReloadService", self)
        _, candidate = self._preflight_source_edit(source_key, request)
        return GatewayConfigSourcePreflightDTO(
            document=self.get_source_document(source_key=source_key),
            candidate=cast(dict[str, object], redact_source_payload(candidate.payload)), candidate_digest=candidate.revision,
            changed_paths=list(changed_json_paths(service.config.payload, candidate.payload,
                             array_identity_keys=gateway_config_policy().array_identity_keys())),
        )

    def _candidate_identity(self, candidate: GatewayConfig, idempotency_key: str | None, source_edit: dict[str, object] | None) -> tuple[str, str]:
        service = cast("GatewayConfigReloadService", self)
        _, generation, _, _ = service._source_baseline(candidate)
        if source_edit is not None:
            if idempotency_key is None:
                raise ValueError("Gateway 来源提交必须绑定原幂等键")
            return "candidate_source_" + sha256(idempotency_key.encode()).hexdigest(), idempotency_key
        return f"candidate_{generation}_{candidate.revision}", idempotency_key or f"reload:{generation}:{candidate.revision}"

    def _prepare_candidate(self, candidate: GatewayConfig, *, idempotency_key: str | None = None, source_edit: dict[str, object] | None = None) -> ConfigPendingCandidateRecord:
        service = cast("GatewayConfigReloadService", self)
        baseline, generation, _, _ = service._source_baseline(candidate)
        payload = prepare_config_for_persistence(candidate.payload)
        if not isinstance(payload, dict):
            raise TypeError("Gateway candidate payload 必须是对象")
        candidate_id, key = self._candidate_identity(candidate, idempotency_key, source_edit)
        store = service._state_store
        existing = store.get_pending_config_candidate(config_domain=service._CONFIG_DOMAIN, candidate_id=candidate_id)
        if source_edit is not None and existing is not None:
            return store.bind_source_edit_config_candidate(candidate=existing, payload=payload, source_baseline=baseline,
                candidate_digest=candidate.revision, source_generation=generation, source_edit=source_edit)
        active = store.get_active_config_snapshot(service._CONFIG_DOMAIN)
        return store.create_pending_config_candidate(
            config_domain=service._CONFIG_DOMAIN, candidate_id=candidate_id, idempotency_key=key, payload=payload,
            source_baseline=baseline, candidate_digest=candidate.revision, effective_digest=candidate.revision,
            target_generation="gateway-runtime", fencing_token=None, state="candidate_validated",
            base_active_revision=active.active_revision if active else None, source_generation=generation, source_edit=source_edit,
        )

    def _saved_source_edit_candidate(self, candidate: GatewayConfig) -> ConfigPendingCandidateRecord | None:
        service = cast("GatewayConfigReloadService", self)
        store = service._state_store
        pending = store.list_unfinished_source_edit_candidates(config_domain=service._CONFIG_DOMAIN, candidate_digest=candidate.revision)
        active = store.get_active_config_snapshot(service._CONFIG_DOMAIN)
        if not pending or active is None:
            return None
        baseline, _, _, _ = service._source_baseline(candidate)
        payload = prepare_config_for_persistence(candidate.payload)
        for record in pending:
            intent = record.source_edit
            if intent is None or intent.get("gateway_id") != service._gateway_id:
                continue
            key = str(intent["source_key"])
            selected = baseline.get(key)
            file = read_stable_config_file(self._source_path(key))
            if not isinstance(selected, dict) or file.digest != intent["file_digest"]:
                continue
            revision = intent["base_layer_revision"]
            target_revision = (int(revision) if isinstance(revision, int) else 0) + (intent["base_layer_digest"] != intent["file_digest"])
            if (active.active_revision, active.effective_digest) != (intent["expected_active_revision"], intent["expected_active_digest"]):
                continue
            if (selected.get("layer_revision"), selected.get("layer_digest")) != (target_revision, intent["file_digest"]):
                continue
            if record.payload == payload and all(source == key or baseline.get(source) == value for source, value in record.source_baseline.items()):
                verify_stable_config_file(file)
                return record
        return None

    async def save_source_edit(
        self, *, source_key: str, request: GatewayConfigSourceEditRequest, before_write: Callable[[], None] | None = None,
        authorize_candidate: Callable[[GatewayConfig], None] | None = None,
    ) -> GatewayConfigSourceDocumentDTO:
        service = cast("GatewayConfigReloadService", self)
        path, store = self._source_path(source_key), service._state_store
        if source_key == "inline:0":
            raise ValueError("Gateway inline 来源只读")
        request_digest = config_revision(request.model_dump())
        pending = store.get_pending_config_candidate(config_domain=service._CONFIG_DOMAIN, idempotency_key=request.idempotency_key)
        if pending is not None:
            if pending.source_edit is None or pending.source_edit.get("request_digest") != request_digest:
                raise ConfigConflictError("Gateway 来源幂等键已绑定其它请求")
            if pending.state != "candidate_validated" or store.get_config_apply_claim(config_domain=service._CONFIG_DOMAIN) is not None:
                return self.get_source_document(source_key=source_key, idempotency_key=request.idempotency_key)
        previous_file = read_stable_config_file(self._source_path(source_key))
        previous_source = parse_source_text(
            previous_file.raw_bytes.decode("utf-8") if previous_file.raw_bytes is not None else "{}\n"
        )
        has_private_inputs = bool(private_input_references(previous_source))
        remove_released_inputs = service._remove_released_plugin_private_inputs
        if has_private_inputs and remove_released_inputs is None:
            raise RuntimeError("Gateway 插件私有输入回收 owner 未装配，拒绝修改来源")
        text, candidate = self._preflight_source_edit(source_key, request, pending)
        verify_stable_config_file(previous_file)
        intent: dict[str, object] = {
            "gateway_id": request.gateway_id, "source_key": source_key, "request_digest": request_digest,
            "file_digest": sha256(text.encode()).hexdigest(), "stage": "accepted",
            "base_layer_revision": request.base_layer_revision, "base_layer_digest": request.base_layer_digest,
            "expected_active_revision": request.expected_active_revision, "expected_active_digest": request.expected_active_digest,
        }

        def save() -> bool:
            if before_write is not None:
                before_write()
            with portalocker.Lock(str(path) + ".lock", mode="a", flags=portalocker.LOCK_EX | portalocker.LOCK_NB):
                accepted = store.get_pending_config_candidate(config_domain=service._CONFIG_DOMAIN, idempotency_key=request.idempotency_key)
                claim = store.get_config_apply_claim(config_domain=service._CONFIG_DOMAIN)
                if accepted is not None:
                    if accepted.source_edit is None or accepted.source_edit.get("request_digest") != request_digest:
                        raise ConfigConflictError("Gateway 来源幂等键已绑定其它请求")
                    if accepted.state != "candidate_validated" or claim is not None:
                        return False
                elif claim is not None:
                    raise ConfigConflictError("已有 Gateway apply claim，禁止接纳另一来源意图")
                file = self._check_source_edit(source_key, request, accepted)
                self._verify_source_dependencies(candidate)
                # 已持有原 reload 准入；包卸载不能插入候选校验与提交之间。
                if authorize_candidate is not None:
                    authorize_candidate(candidate)
                if accepted is None:
                    self._prepare_candidate(candidate, idempotency_key=request.idempotency_key, source_edit=intent)
                verify_stable_config_file(file)
                if file.digest != intent["file_digest"]:
                    if before_write is not None:
                        before_write()
                    self._verify_source_dependencies(candidate)
                    atomic_write_bytes(file.path, text.encode())
                if has_private_inputs and remove_released_inputs is not None:
                    remove_released_inputs(previous_source)
                return True

        await service.reload(before_build=save, idempotency_key=request.idempotency_key, source_edit=intent)
        return self.get_source_document(source_key=source_key, idempotency_key=request.idempotency_key)

    def _source_baseline(
        self,
        config: GatewayConfig,
    ) -> tuple[dict[str, object], int, dict[str, int], dict[str, str | None]]:
        baseline: dict[str, object] = {}
        revisions: dict[str, int] = {}
        digests: dict[str, str | None] = {}
        source_generation = 0
        for source in config.source_details:
            key = source.source_key or f"{source.layer}:{source.precedence}"
            baseline[key] = {
                "uri": source.uri,
                "presence": source.presence,
                "layer_revision": source.layer_revision,
                "layer_digest": source.layer_digest,
                "source_generation": source.source_generation,
                "layer": source.layer,
                "precedence": source.precedence,
                "document": (
                    thaw(config.graph.source_documents[source.uri])
                    if config.graph is not None and source.uri in config.graph.source_documents else None
                ),
            }
            if source.layer_revision is not None:
                revisions[key] = source.layer_revision
            digests[key] = source.layer_digest
            if source.source_generation is not None:
                source_generation = max(source_generation, source.source_generation)
        return baseline, source_generation, revisions, digests
