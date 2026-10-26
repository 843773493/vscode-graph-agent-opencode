"""由 canonical 发现结果与封存来源派生活动定义；正文仍归原 activation。"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.agents.policy.tool_capability import ToolCapabilityPolicy, ToolMetadata
from app.agents.tool_identity import TOOL_DISCOVERY_NAME
from app.domain.itemized.assembly_snapshot import ContextAssemblySnapshot
from app.domain.itemized.epoch.source_protocol import parse_source_blocks
from app.domain.itemized.hashing import canonical_json_bytes, sha256_jcs
from app.domain.itemized.records import CanonicalItemRecord
from app.services.infrastructure.mcp.extension_catalog import (
    ExtensionCatalogUnavailableError,
)
from app.services.infrastructure.mcp.tool_definitions import (
    FrozenToolCatalog,
    FrozenToolDefinition,
    definition_resource_id,
)
from app.services.infrastructure.rollout_context.runtime.context_sources.models import (
    ContextSourceDelta,
    ContextSourceDescriptor,
    ContextSourceSnapshot,
    PendingContextSourceBatch,
)
from app.services.mapping.itemized.canonical_langchain import project_tool_result

TOOL_DEFINITION_SOURCE_KIND = "extension_tool_definition"


def is_tool_definition_source(source_id: str | None) -> bool:
    return isinstance(source_id, str) and source_id.startswith("extension-tool-definition:v1:")


class DefinitionProvenance(BaseModel, frozen=True):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    activation_snapshot_id: str = Field(min_length=1)
    resource_id: str = Field(min_length=1)
    revision: str = Field(min_length=1)


class DiscoveredToolRef(DefinitionProvenance, frozen=True):
    tool_id: str = Field(min_length=1)
    tool_call_id: str = Field(min_length=1)
    model_call_id: str = Field(min_length=1)


class RestoredDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    description: str
    input_schema: dict[str, object]
    kind: str
    group_id: str
    schema_hash: str
    provenance: DefinitionProvenance

    def payload(self) -> dict[str, object]:
        return self.model_dump(exclude={"schema_hash", "provenance"})


class DefinitionSourceValue(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    tool_id: str
    status: Literal["available", "unavailable"]
    discovered_from: DiscoveredToolRef
    definition: RestoredDefinition | None = None
    reason: Literal["tool-disabled", "tool-removed"] | None = None


@dataclass(frozen=True, slots=True)
class DiscoveryEntry:
    ref: DiscoveredToolRef
    inline_payload: Mapping[str, object] | None
    schema_hash: str


def discovery_entries(items: Sequence[CanonicalItemRecord]) -> Iterator[DiscoveryEntry]:
    """只认实际成功的 search_tools 结果及其 canonical model-call 身份。"""
    for item in items:
        if item.semantic_kind != "tool_result":
            continue
        message = project_tool_result(item)
        if message.name != TOOL_DISCOVERY_NAME or message.status != "success":
            continue
        identity = item.projection_identity
        if identity is None or identity.model_call_id is None:
            raise ExtensionCatalogUnavailableError("成功发现结果缺少 canonical model-call 身份")
        try:
            if not isinstance(message.content, str):
                raise TypeError("成功发现结果必须是 JSON 文本")
            payload = json.loads(message.content)
            if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
                raise TypeError("缺少有序 results")
            for entry in payload["results"]:
                if not isinstance(entry, dict):
                    raise TypeError("发现结果条目必须是对象")
                if entry.get("status") != "available":
                    continue
                provenance = DefinitionProvenance.model_validate(entry["provenance"])
                ref = DiscoveredToolRef(
                    **provenance.model_dump(), tool_id=entry["tool_id"],
                    tool_call_id=message.tool_call_id, model_call_id=identity.model_call_id,
                )
                if ref.resource_id != definition_resource_id(ref.tool_id):
                    raise ValueError("工具与定义资源身份不一致")
                state = entry["schema_state"]
                if state not in {"inline_complete", "needs_read"}:
                    raise ValueError("未知 schema_state")
                inline = {key: entry[key] for key in ("description", "input_schema", "kind", "group_id")} if state == "inline_complete" else None
                yield DiscoveryEntry(ref, inline, str(entry["schema_hash"]))
        except (KeyError, TypeError, ValueError) as error:
            raise ExtensionCatalogUnavailableError("成功 search_tools 结果或 provenance 损坏") from error


def definition_source_values(
    assembly: ContextAssemblySnapshot | None,
    retained: frozenset[tuple[str, str]] | None = None,
) -> Iterator[tuple[DefinitionSourceValue, str]]:
    if assembly is None or assembly.seal_binding is None:
        return
    for frame in assembly.seal_binding.prefix.state.frames:
        if not any(is_tool_definition_source(ref.source_identity) for ref in frame.item_refs):
            continue
        blocks = parse_source_blocks(frame.content, expected_version=frame.source_protocol)
        for ref, block in zip(frame.item_refs, blocks, strict=True):
            if not is_tool_definition_source(ref.source_identity):
                continue
            if retained is not None and (ref.ref_id, ref.source_revision) not in retained:
                continue
            if (block.source_kind != TOOL_DEFINITION_SOURCE_KIND
                    or frame.role != "user" or frame.payload_kind != "text"
                    or block.root_placement != "tail_only"
                    or ref.source_tracking_at_seal != "untracked"):
                raise ExtensionCatalogUnavailableError("活动定义缺少唯一封存来源块")
            if not isinstance(block.body, str):
                raise ExtensionCatalogUnavailableError("活动定义来源必须是 JSON 文本")
            try:
                value = DefinitionSourceValue.model_validate_json(block.body)
            except ValidationError as error:
                raise ExtensionCatalogUnavailableError("活动定义来源正文损坏") from error
            if (value.discovered_from.resource_id != definition_resource_id(value.tool_id)
                    or value.tool_id != value.discovered_from.tool_id
                    or ref.source_identity != definition_resource_id(value.tool_id)
                    or ref.source_revision != sha256_jcs({"content": block.body})
                    or (value.status == "available") != (value.definition is not None)
                    or (value.status == "unavailable") != (value.reason is not None)):
                raise ExtensionCatalogUnavailableError("活动定义来源正文或身份冲突")
            assert ref.source_revision is not None
            yield value, ref.source_revision


def tool_definition_batch(
    batch: PendingContextSourceBatch, *,
    catalog_supplier: Callable[[], FrozenToolCatalog],
    current_items: Sequence[CanonicalItemRecord],
    retained_items: Sequence[CanonicalItemRecord],
    previous: ContextAssemblySnapshot | None,
    retained_assembly: ContextAssemblySnapshot | None,
    retained_source_refs: frozenset[tuple[str, str]] | None,
    resolve_discovery: Callable[[DiscoveredToolRef], FrozenToolDefinition],
    policies_for: Callable[[Sequence[ToolMetadata]], Mapping[str, ToolCapabilityPolicy]] | None,
    automatic_tool_ids: frozenset[str],
    restoring: bool,
) -> PendingContextSourceBatch:
    """不建活动状态表；只产生既有 Saver 提交的来源 batch。"""
    discoveries: dict[str, DiscoveredToolRef] = {}
    prior_sources: dict[str, DefinitionSourceValue] = {}
    for value, _revision in definition_source_values(retained_assembly, retained_source_refs):
        discoveries[value.tool_id] = value.discovered_from
        prior_sources[value.tool_id] = value
    entries = tuple(discovery_entries((*retained_items, *current_items)))
    for entry in entries:
        prior = discoveries.get(entry.ref.tool_id)
        if prior is not None and prior != entry.ref:
            prior_sources.pop(entry.ref.tool_id, None)
        discoveries[entry.ref.tool_id] = entry.ref
    if not discoveries:
        return batch
    if policies_for is None:
        raise ExtensionCatalogUnavailableError("发现定义恢复缺少完整策略解析器")
    originals = {name: resolve_discovery(ref) for name, ref in discoveries.items()}
    for entry in entries:
        original = resolve_discovery(entry.ref)
        if (entry.schema_hash != original.schema_hash or entry.inline_payload is not None
                and canonical_json_bytes(entry.inline_payload) != canonical_json_bytes(original.payload)):
            raise ExtensionCatalogUnavailableError("发现结果正文与原冻结定义不一致")
    catalog = catalog_supplier()
    current = {definition.metadata.tool_id: definition for definition in catalog.definitions}
    policies = policies_for(tuple(current.get(name, definition).metadata for name, definition in originals.items()))
    if set(policies) != set(originals):
        raise ExtensionCatalogUnavailableError("定义恢复缺少完整有效策略")
    current_entries = {entry.ref for entry in discovery_entries(current_items)}
    inline_refs = {entry.ref for entry in entries if entry.inline_payload is not None}
    previous_revisions = {value.tool_id: revision for value, revision in definition_source_values(previous)}
    sources, deltas = list(batch.sources), list(batch.deltas)
    for name, discovered in sorted(discoveries.items()):
        policy = policies[name]
        if policy.model_visibility_locked:
            continue
        definition = current.get(name)
        original, prior = originals[name], prior_sources.get(name)
        if definition is not None and policy.execution_enabled:
            if name in automatic_tool_ids:
                continue
            # 普通续读仍使用原内联／artifact；仅原正文被移除或契约更新时恢复。
            if (prior is None and definition.provenance.revision == original.provenance.revision
                    and discovered in current_entries and (discovered in inline_refs or not restoring)):
                continue
            if (prior is not None and prior.definition is not None
                    and prior.definition.provenance.revision == definition.provenance.revision):
                value = prior
            else:
                unchanged = definition.provenance.revision == original.provenance.revision
                definition = original if unchanged else definition
                value = DefinitionSourceValue(
                    tool_id=name, status="available", discovered_from=discovered,
                    definition=RestoredDefinition.model_validate({
                        **definition.payload, "schema_hash": definition.schema_hash,
                        "provenance": DefinitionProvenance(
                            activation_snapshot_id=discovered.activation_snapshot_id if unchanged else catalog.activation_snapshot_id,
                            resource_id=definition.provenance.resource_id, revision=definition.provenance.revision,
                        ),
                    }),
                )
        else:
            value = DefinitionSourceValue(
                tool_id=name, status="unavailable", discovered_from=discovered,
                reason="tool-disabled" if not policy.execution_enabled else "tool-removed",
            )
        content = canonical_json_bytes(value.model_dump(exclude_none=True)).decode()
        revision = sha256_jcs({"content": content})
        previous_revision = previous_revisions.get(name)
        source_id = discovered.resource_id
        if any(source.descriptor.source_id == source_id for source in batch.sources):
            raise ExtensionCatalogUnavailableError("活动定义不得登记第二个 CSM owner")
        sources.append(ContextSourceSnapshot(
            descriptor=ContextSourceDescriptor(
                source_id=source_id, source_kind=TOOL_DEFINITION_SOURCE_KIND,
                name=f"扩展工具 {name}", description="已发现定义的完整替换或删除事实",
                internal_locator=source_id, root_placement="tail_only",
            ), revision=revision, content=content, applied_revision=previous_revision, tracked=False,
        ))
        if revision != previous_revision:
            deltas.append(ContextSourceDelta(
                source_id=source_id, source_kind=TOOL_DEFINITION_SOURCE_KIND,
                source_name=f"扩展工具 {name}", revision=revision, previous_revision=previous_revision,
                content=content, content_hash=sha256_jcs(content),
                kind="activation" if previous_revision is None else "delta",
            ))
    return PendingContextSourceBatch(tuple(deltas), tuple(sources))
