"""v2 资源激活 snapshot/provenance 的冻结领域合同。

本模块只定义类型、字段清单和内容 hash 范围，不拥有 ResourceRegistry、
monitor、loader、activation policy，也不做任何 I/O。资源平台在 model-call
preparation 前冻结的 typed snapshot/decision 在此被固化成 ContextStore
可以持久化和恢复的领域事实。

破坏性 schema 清单（字段名即持久化列名，无旧字段 reader、无别名）：

ResourceActivationSnapshotRef
  activation_snapshot_id    snapshot_kind
  parent_turn_snapshot_id   activation_policy_revision
  activation_policy_hash    registry_generation
  owner_session_id          owner_thread_id
  turn_id                   model_call_id
  captured_at               bindings_hash
  activation_provenance_hash
  bindings[]                # ResourceProvenanceRef

ResourceProvenanceRef
  resource_id               display_uri          resource_kind
  owner_scope               facet                revision
  content_length            content_hash         redacted_stable_digest
  snapshot_ref              detail_ref           source_lineage_ref
  source_lineage_digest     availability         activation_ordinal
  effective_boundary        captured_registry_generation

SourceLineageRef
  lineage_id                derivation_version
  sources[]                 digest

两个内容 hash 的语义严格分离：

* bindings_hash 只覆盖按 activation ordinal 排列的实际语义选择；来源 raw
  revision、source lineage、policy、effective boundary 和 captured
  generation 均不进入，因此相同 wire 选择不会因无关观察或 policy 发布抖动。
* activation_provenance_hash 额外覆盖 snapshot kind、parent_kind 与
  parent_bindings_hash 关系描述、policy revision/hash、Registry generation
  以及每个 binding 的 effective boundary、captured generation 和 source
  lineage digest。

运行 identity（activation_snapshot_id、Turn/model-call identity、owner
session/thread）与 captured_at 只作 typed relation，不进入任一内容 hash；
具体 parent_turn_snapshot_id 由 typed parent relation 与 turn-bound binding
逐字节复用校验保护。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Final

from app.domain.itemized.detail_ref import DetailRef
from app.domain.itemized.enums import DetailAvailability
from app.domain.itemized.errors import ItemSchemaError
from app.domain.itemized.hashing import sha256_jcs, validate_hash_token
from app.domain.itemized.refs import require_manifest_token

ResourceActivationBoundary = str
_BOUNDARIES: Final[frozenset[str]] = frozenset({"turn", "model_call"})
_SNAPSHOT_KINDS: Final[frozenset[str]] = _BOUNDARIES
_AVAILABILITIES: Final[frozenset[str]] = frozenset(
    item.value for item in DetailAvailability
)
_TOKEN_PATTERN_ERROR: Final = "必须匹配 ^[a-z][a-z0-9_-]{0,63}$"
_DISPLAY_URI_SCHEME: Final = "boxteam://"
RESOURCE_ACTIVATION_ERROR_CODES: Final[frozenset[str]] = frozenset(
    {
        "resource-activation-schema-invalid",
        "resource-activation-boundary-singularity-rejected",
        "resource-activation-provider-locator-rejected",
        "resource-activation-credential-rejected",
        "resource-activation-absolute-path-rejected",
        "resource-activation-legacy-field-rejected",
        "resource-activation-field-alias-rejected",
        "resource-activation-lineage-invalid",
        "resource-activation-parent-invalid",
        "resource-activation-ordinal-conflict",
        "resource-activation-hash-mismatch",
    }
)


class ResourceActivationContractError(ItemSchemaError):
    """资源激活 snapshot/provenance 违反领域合同；code 是闭合集合。"""

    def __init__(self, code: str, message: str) -> None:
        if code not in RESOURCE_ACTIVATION_ERROR_CODES:
            raise ValueError(f"未知 ResourceActivationContractError code: {code}")
        super().__init__(f"[{code}] {message}")
        self.code = code


RESOURCE_ACTIVATION_SNAPSHOT_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "activation_snapshot_id",
        "snapshot_kind",
        "parent_turn_snapshot_id",
        "activation_policy_revision",
        "activation_policy_hash",
        "registry_generation",
        "owner_session_id",
        "owner_thread_id",
        "turn_id",
        "model_call_id",
        "captured_at",
        "bindings_hash",
        "activation_provenance_hash",
        "bindings",
    }
)

RESOURCE_PROVENANCE_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "resource_id",
        "display_uri",
        "resource_kind",
        "owner_scope",
        "facet",
        "revision",
        "content_length",
        "content_hash",
        "redacted_stable_digest",
        "snapshot_ref",
        "detail_ref",
        "source_lineage_ref",
        "source_lineage_digest",
        "availability",
        "activation_ordinal",
        "effective_boundary",
        "captured_registry_generation",
    }
)

SOURCE_LINEAGE_REF_FIELDS: Final[frozenset[str]] = frozenset(
    {"lineage_id", "derivation_version", "sources", "digest"}
)

# 拒绝的别名字段：旧实现、历史实验 v2 或 Provider 词汇都不得成为第二套读法。
SNAPSHOT_FIELD_ALIASES: Final[Mapping[str, str]] = {
    "snapshot_id": "activation_snapshot_id",
    "kind": "snapshot_kind",
    "policy_revision": "activation_policy_revision",
    "policy_hash": "activation_policy_hash",
    "generation": "registry_generation",
    "session_id": "owner_session_id",
    "thread_id": "owner_thread_id",
    "created_at": "captured_at",
    "parent_id": "parent_turn_snapshot_id",
    "parent_snapshot_id": "parent_turn_snapshot_id",
    "model_call_identity": "model_call_id",
    "provenance_hash": "activation_provenance_hash",
}

PROVENANCE_FIELD_ALIASES: Final[Mapping[str, str]] = {
    "kind": "resource_kind",
    "semantic_revision": "revision",
    "revision_hash": "content_hash",
    "uri": "display_uri",
    "resource_uri": "display_uri",
    "scope": "owner_scope",
    "length": "content_length",
    "hash": "content_hash",
    "ordinal": "activation_ordinal",
    "boundary": "effective_boundary",
    "registry_generation": "captured_registry_generation",
    "snapshot_digest": "source_lineage_digest",
    "lineage_ref": "source_lineage_ref",
    "lineage_digest": "source_lineage_digest",
    "revision_set": "source_lineage_ref",
}

# 单值 boundary：assembly 级的唯一 boundary 字段必须被显式拒绝，因为同一
# assembly 允许混合 turn/model_call binding。
BOUNDARY_SINGULARITY_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "boundary",
        "assembly_boundary",
        "assembly_effective_boundary",
        "snapshot_boundary",
        "default_boundary",
        "boundaries",
    }
)

# provider locator / endpoint / 内部 handle 永不得进入内容事实。
PROVIDER_LOCATOR_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "provider_locator",
        "provider_uri",
        "provider_path",
        "endpoint",
        "network_endpoint",
        "server_endpoint",
        "server_id",
        "connection_id",
        "handle",
        "internal_handle",
        "snapshot_handle",
        "detail_handle",
        "mcp_server",
    }
)

# 凭据绝不进入 snapshot/provenance/JSONL/history。
CREDENTIAL_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "credential",
        "credentials",
        "credential_ref",
        "credential_id",
        "api_key",
        "apikey",
        "token",
        "access_token",
        "refresh_token",
        "secret",
        "client_secret",
        "password",
        "authorization",
    }
)

# 绝对路径与文件 locator 形态只属于资源 owner 私有侧。
ABSOLUTE_PATH_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "path",
        "file_path",
        "absolute_path",
        "abs_path",
        "storage_path",
        "locator",
        "physical_locator",
        "workspace_path",
    }
)

# 旧路径 / 旧推断字段：正常 runtime 不提供 reader、alias 或 fallback。
LEGACY_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "read_file_path",
        "resource_path",
        "source_path",
        "boxteam_path",
        "agents_path",
        "skill_path",
        "prompt_replay",
        "cutoff_index",
        "metadata",
        "effective_boundary_v1",
        "snapshot_kind_v1",
    }
)


def _schema_error(message: str) -> ResourceActivationContractError:
    return ResourceActivationContractError(
        "resource-activation-schema-invalid", message
    )


def _non_empty_string(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value:
        raise _schema_error(f"{field_name} 必须是非空字符串")
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in value):
        raise _schema_error(f"{field_name} 不得携带控制字符")
    return value


def _non_negative_int(value: object, field_name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise _schema_error(f"{field_name} 必须是非负整数")
    return value


def _token(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value:
        raise _schema_error(f"{field_name} 必须是非空字符串")
    if (
        value[0] not in "abcdefghijklmnopqrstuvwxyz"
        or len(value) > 64
        or any(
            not (char.isascii() and (char.islower() or char.isdigit()))
            and char not in "_-"
            for char in value
        )
    ):
        raise _schema_error(f"{field_name} {_TOKEN_PATTERN_ERROR}: {value!r}")
    return value


def _reject_path_or_credential_shape(value: str, field_name: str) -> None:
    """拒绝 identity/URI 值上的路径、locator、凭据与旧路径形态。"""

    if "read_file_path" in value or ".boxteam" in value:
        raise ResourceActivationContractError(
            "resource-activation-legacy-field-rejected",
            f"{field_name} 是旧路径形态，正常 runtime 不得读取: {value!r}",
        )
    if value.startswith(("/", "\\")) or (
        len(value) >= 3 and value[1] == ":" and value[2] in "\\/"
    ):
        raise ResourceActivationContractError(
            "resource-activation-absolute-path-rejected",
            f"{field_name} 不得是绝对路径: {value!r}",
        )
    if "\\" in value:
        raise ResourceActivationContractError(
            "resource-activation-absolute-path-rejected",
            f"{field_name} 不得携带路径分隔符: {value!r}",
        )
    if value.find("://") > 0 and not value.startswith(_DISPLAY_URI_SCHEME):
        raise ResourceActivationContractError(
            "resource-activation-provider-locator-rejected",
            f"{field_name} 不得是 provider locator/endpoint: {value!r}",
        )
    if "@" in value:
        raise ResourceActivationContractError(
            "resource-activation-credential-rejected",
            f"{field_name} 不得携带 userinfo/credential 形态: {value!r}",
        )
    if "%" in value:
        raise _schema_error(f"{field_name} 拒绝百分号编码: {value!r}")


def _safe_identity(value: object, field_name: str) -> str:
    text = _non_empty_string(value, field_name)
    _reject_path_or_credential_shape(text, field_name)
    if any(marker in text for marker in ("/", "\\", "?", "#", " ")):
        raise ResourceActivationContractError(
            "resource-activation-provider-locator-rejected",
            f"{field_name} 是内部稳定 identity，不得携带路径/URI 形态: {text!r}",
        )
    if not text.isascii():
        raise _schema_error(f"{field_name} 必须是纯 ASCII")
    return text


def _display_uri(value: object, field_name: str) -> str:
    """校验模型可见的安全虚拟 URI；它不是 identity，也不得解析回当前资源。

    完整 VRN grammar 由资源平台单一 owner 维护，本模块只校验 provenance
    安全投影不变量，避免在 domain 复制第二套 URI 语法。
    """

    text = _non_empty_string(value, field_name)
    _reject_path_or_credential_shape(text, field_name)
    if not text.isascii():
        raise _schema_error(f"{field_name} 必须是纯 ASCII")
    if not text.startswith(_DISPLAY_URI_SCHEME):
        raise ResourceActivationContractError(
            "resource-activation-provider-locator-rejected",
            f"{field_name} 必须是 {_DISPLAY_URI_SCHEME} 逻辑地址: {text!r}",
        )
    for marker in ("?", "#"):
        if marker in text:
            raise _schema_error(f"{field_name} 不得携带 query/fragment: {text!r}")
    segments = text[len(_DISPLAY_URI_SCHEME):].split("/")
    if any(segment in ("", ".", "..") for segment in segments):
        raise _schema_error(
            f"{field_name} 不得有空的或 . / .. segment: {text!r}"
        )
    return text


def _reject_forbidden_fields(
    raw: Mapping[str, object],
    *,
    allowed: frozenset[str],
    aliases: Mapping[str, str],
    where: str,
) -> None:
    """对未登记字段做显式分类拒绝；不提供任何别名/旧字段读法。"""

    for key in raw:
        if key in allowed:
            continue
        if key in BOUNDARY_SINGULARITY_FIELDS:
            raise ResourceActivationContractError(
                "resource-activation-boundary-singularity-rejected",
                f"{where} 不得有 assembly 单值 boundary 字段: {key!r}",
            )
        if key in CREDENTIAL_FIELDS:
            raise ResourceActivationContractError(
                "resource-activation-credential-rejected",
                f"{where} 不得携带 credential 字段: {key!r}",
            )
        if key in PROVIDER_LOCATOR_FIELDS:
            raise ResourceActivationContractError(
                "resource-activation-provider-locator-rejected",
                f"{where} 不得携带 provider locator/handle 字段: {key!r}",
            )
        if key in ABSOLUTE_PATH_FIELDS:
            raise ResourceActivationContractError(
                "resource-activation-absolute-path-rejected",
                f"{where} 不得携带绝对路径/locator 字段: {key!r}",
            )
        if key in LEGACY_FIELDS:
            raise ResourceActivationContractError(
                "resource-activation-legacy-field-rejected",
                f"{where} 不得携带旧字段: {key!r}",
            )
        if key in aliases:
            raise ResourceActivationContractError(
                "resource-activation-field-alias-rejected",
                f"{where} 不得使用别名字段 {key!r}，规范字段是 {aliases[key]!r}",
            )
        raise _schema_error(f"{where} 含未登记字段: {key!r}")


def _require_pair(value: object) -> tuple[object, object]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise ResourceActivationContractError(
            "resource-activation-lineage-invalid",
            "source_lineage_ref.sources 每条必须是 [source_id, revision]",
        )
    if len(value) != 2:
        raise ResourceActivationContractError(
            "resource-activation-lineage-invalid",
            "source_lineage_ref.sources 每条必须恰有两个元素",
        )
    return value[0], value[1]


@dataclass(frozen=True, slots=True)
class SourceLineageRef:
    """受保护的 source 来源 manifest：source_id/revision 向量与派生版本。

    只承载来源身份和派生版本，不含 provider locator/credential/路径；digest
    由来源向量与 derivation 版本确定性导出，进入 activation_provenance_hash
    但不进入 bindings_hash。该 ref 不允许据此重新读取当前来源。
    """

    lineage_id: str
    derivation_version: str
    sources: tuple[tuple[str, str], ...]
    digest: str = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        _safe_identity(self.lineage_id, "SourceLineageRef.lineage_id")
        _non_empty_string(
            self.derivation_version, "SourceLineageRef.derivation_version"
        )
        if not isinstance(self.sources, tuple):
            raise ResourceActivationContractError(
                "resource-activation-lineage-invalid",
                "SourceLineageRef.sources 必须是元组",
            )
        seen: set[str] = set()
        for entry in self.sources:
            if not isinstance(entry, tuple) or len(entry) != 2:
                raise ResourceActivationContractError(
                    "resource-activation-lineage-invalid",
                    "SourceLineageRef.sources 必须是 (source_id, revision) 元组",
                )
            source_id = _safe_identity(entry[0], "SourceLineageRef.source_id")
            _non_empty_string(entry[1], "SourceLineageRef.revision")
            if source_id in seen:
                raise ResourceActivationContractError(
                    "resource-activation-lineage-invalid",
                    f"SourceLineageRef 重复 source_id: {source_id!r}",
                )
            seen.add(source_id)
        object.__setattr__(
            self,
            "digest",
            sha256_jcs(
                {
                    "schema": "source-lineage-digest:v1",
                    "derivation_version": self.derivation_version,
                    "sources": [
                        [source_id, revision]
                        for source_id, revision in sorted(self.sources)
                    ],
                }
            ),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "lineage_id": self.lineage_id,
            "derivation_version": self.derivation_version,
            "sources": [
                [source_id, revision] for source_id, revision in self.sources
            ],
            "digest": self.digest,
        }

    @classmethod
    def from_dict(cls, value: object) -> SourceLineageRef:
        if not isinstance(value, Mapping):
            raise _schema_error("source_lineage_ref 必须是 typed object")
        _reject_forbidden_fields(
            value,
            allowed=SOURCE_LINEAGE_REF_FIELDS,
            aliases={"ref": "source_lineage_ref", "revision_set": "sources"},
            where="source_lineage_ref",
        )
        missing = SOURCE_LINEAGE_REF_FIELDS - set(value)
        if missing:
            raise _schema_error(
                f"source_lineage_ref 缺少必填字段: {sorted(missing)}"
            )
        raw_sources = value["sources"]
        if not isinstance(raw_sources, Sequence) or isinstance(
            raw_sources, (str, bytes)
        ):
            raise _schema_error("source_lineage_ref.sources 必须是数组")
        lineage = cls(
            lineage_id=value["lineage_id"],
            derivation_version=value["derivation_version"],
            sources=tuple(
                (_require_pair(item)[0], _require_pair(item)[1])
                for item in raw_sources
            ),
        )
        if value["digest"] != lineage.digest:
            raise ResourceActivationContractError(
                "resource-activation-hash-mismatch",
                "source_lineage_ref.digest 与来源向量/派生版本不一致",
            )
        return lineage


@dataclass(frozen=True, slots=True)
class ResourceProvenanceRef:
    """一条已激活语义资源的 provenance；不含 payload、locator 或凭据。"""

    resource_id: str
    display_uri: str
    resource_kind: str
    owner_scope: str
    facet: str
    revision: str
    availability: str
    content_length: int
    content_hash: str | None
    redacted_stable_digest: str | None
    source_lineage_ref: SourceLineageRef
    source_lineage_digest: str
    activation_ordinal: int
    effective_boundary: ResourceActivationBoundary
    captured_registry_generation: int
    snapshot_ref: DetailRef | None = None
    detail_ref: DetailRef | None = None

    def __post_init__(self) -> None:
        _safe_identity(self.resource_id, "ResourceProvenanceRef.resource_id")
        _display_uri(self.display_uri, "ResourceProvenanceRef.display_uri")
        _token(self.resource_kind, "ResourceProvenanceRef.resource_kind")
        _token(self.owner_scope, "ResourceProvenanceRef.owner_scope")
        _token(self.facet, "ResourceProvenanceRef.facet")
        _non_empty_string(self.revision, "ResourceProvenanceRef.revision")
        if self.availability not in _AVAILABILITIES:
            raise _schema_error(
                f"未知 ResourceProvenanceRef.availability: {self.availability!r}"
            )
        _non_negative_int(
            self.content_length, "ResourceProvenanceRef.content_length"
        )
        if not isinstance(self.source_lineage_ref, SourceLineageRef):
            raise _schema_error(
                "ResourceProvenanceRef.source_lineage_ref 必须是 SourceLineageRef"
            )
        if self.source_lineage_digest != self.source_lineage_ref.digest:
            raise ResourceActivationContractError(
                "resource-activation-hash-mismatch",
                "source_lineage_digest 与 source_lineage_ref 不一致",
            )
        _non_negative_int(
            self.activation_ordinal, "ResourceProvenanceRef.activation_ordinal"
        )
        if self.effective_boundary not in _BOUNDARIES:
            raise _schema_error(
                "ResourceProvenanceRef.effective_boundary 必须是 turn|model_call"
            )
        _non_negative_int(
            self.captured_registry_generation,
            "ResourceProvenanceRef.captured_registry_generation",
        )
        for name in ("snapshot_ref", "detail_ref"):
            ref = getattr(self, name)
            if ref is not None and not isinstance(ref, DetailRef):
                raise _schema_error(
                    f"ResourceProvenanceRef.{name} 必须是 typed DetailRef"
                )
        if (self.snapshot_ref is None) == (self.detail_ref is None):
            raise _schema_error(
                "ResourceProvenanceRef 必须恰好携带 snapshot_ref 或 detail_ref"
            )
        require_manifest_token(self)

    def to_dict(self) -> dict[str, object]:
        return {
            "resource_id": self.resource_id,
            "display_uri": self.display_uri,
            "resource_kind": self.resource_kind,
            "owner_scope": self.owner_scope,
            "facet": self.facet,
            "revision": self.revision,
            "content_length": self.content_length,
            "content_hash": self.content_hash,
            "redacted_stable_digest": self.redacted_stable_digest,
            "snapshot_ref": (
                self.snapshot_ref.to_dict() if self.snapshot_ref is not None else None
            ),
            "detail_ref": (
                self.detail_ref.to_dict() if self.detail_ref is not None else None
            ),
            "source_lineage_ref": self.source_lineage_ref.to_dict(),
            "source_lineage_digest": self.source_lineage_digest,
            "availability": self.availability,
            "activation_ordinal": self.activation_ordinal,
            "effective_boundary": self.effective_boundary,
            "captured_registry_generation": self.captured_registry_generation,
        }

    @classmethod
    def from_dict(cls, value: object) -> ResourceProvenanceRef:
        if not isinstance(value, Mapping):
            raise _schema_error("resource provenance 必须是 typed object")
        _reject_forbidden_fields(
            value,
            allowed=RESOURCE_PROVENANCE_FIELDS,
            aliases=PROVENANCE_FIELD_ALIASES,
            where="ResourceProvenanceRef",
        )
        missing = RESOURCE_PROVENANCE_FIELDS - set(value)
        if missing:
            raise _schema_error(
                f"ResourceProvenanceRef 缺少必填字段: {sorted(missing)}"
            )
        lineage_raw = value["source_lineage_ref"]
        lineage = (
            lineage_raw
            if isinstance(lineage_raw, SourceLineageRef)
            else SourceLineageRef.from_dict(lineage_raw)
        )
        refs: dict[str, DetailRef | None] = {}
        for name in ("snapshot_ref", "detail_ref"):
            raw_ref = value[name]
            if raw_ref is None or isinstance(raw_ref, DetailRef):
                refs[name] = raw_ref
            else:
                refs[name] = DetailRef.from_dict(raw_ref)
        return cls(
            resource_id=value["resource_id"],
            display_uri=value["display_uri"],
            resource_kind=value["resource_kind"],
            owner_scope=value["owner_scope"],
            facet=value["facet"],
            revision=value["revision"],
            availability=value["availability"],
            content_length=value["content_length"],
            content_hash=value["content_hash"],
            redacted_stable_digest=value["redacted_stable_digest"],
            source_lineage_ref=lineage,
            source_lineage_digest=value["source_lineage_digest"],
            activation_ordinal=value["activation_ordinal"],
            effective_boundary=value["effective_boundary"],
            captured_registry_generation=value["captured_registry_generation"],
            snapshot_ref=refs["snapshot_ref"],
            detail_ref=refs["detail_ref"],
        )


def _ordered_bindings(
    bindings: Sequence[ResourceProvenanceRef],
) -> tuple[ResourceProvenanceRef, ...]:
    result = tuple(bindings)
    for binding in result:
        if not isinstance(binding, ResourceProvenanceRef):
            raise _schema_error("bindings 必须是 ResourceProvenanceRef 元组")
    ordinals = [binding.activation_ordinal for binding in result]
    if len(set(ordinals)) != len(ordinals):
        raise ResourceActivationContractError(
            "resource-activation-ordinal-conflict",
            "activation_ordinal 在同一 snapshot 内必须唯一",
        )
    return tuple(sorted(result, key=lambda binding: binding.activation_ordinal))


def resource_bindings_hash(bindings: Sequence[ResourceProvenanceRef]) -> str:
    """只覆盖按 activation ordinal 排列的实际有序语义选择。"""

    return sha256_jcs(
        {
            "schema": "resource-activation-bindings:v1",
            "bindings": [
                {
                    "resource_id": binding.resource_id,
                    "resource_kind": binding.resource_kind,
                    "owner_scope": binding.owner_scope,
                    "facet": binding.facet,
                    "revision": binding.revision,
                    "content_length": binding.content_length,
                    "content_hash": binding.content_hash,
                    "redacted_stable_digest": binding.redacted_stable_digest,
                    "availability": binding.availability,
                    "display_uri": binding.display_uri,
                }
                for binding in _ordered_bindings(bindings)
            ],
        }
    )


def resource_activation_provenance_hash(
    *,
    snapshot_kind: str,
    parent: ResourceActivationSnapshotRef | None,
    activation_policy_revision: str,
    activation_policy_hash: str,
    registry_generation: int,
    bindings: Sequence[ResourceProvenanceRef],
) -> str:
    """覆盖 policy/capture/parent 关系与 source lineage 的独立 provenance hash。"""

    return sha256_jcs(
        {
            "schema": "resource-activation-provenance:v1",
            "snapshot_kind": snapshot_kind,
            "parent_snapshot_kind": None if parent is None else parent.snapshot_kind,
            "parent_bindings_hash": None if parent is None else parent.bindings_hash,
            "activation_policy_revision": activation_policy_revision,
            "activation_policy_hash": activation_policy_hash,
            "registry_generation": registry_generation,
            "bindings": [
                {
                    "resource_id": binding.resource_id,
                    "effective_boundary": binding.effective_boundary,
                    "captured_registry_generation": (
                        binding.captured_registry_generation
                    ),
                    "source_lineage_digest": binding.source_lineage_digest,
                }
                for binding in _ordered_bindings(bindings)
            ],
        }
    )


@dataclass(frozen=True, slots=True)
class ResourceActivationSnapshotRef:
    """一个 Turn 或一次 model call 冻结的资源激活结果。"""

    activation_snapshot_id: str
    snapshot_kind: str
    activation_policy_revision: str
    activation_policy_hash: str
    registry_generation: int
    owner_session_id: str
    owner_thread_id: str
    turn_id: str
    captured_at: str
    bindings: tuple[ResourceProvenanceRef, ...]
    parent: ResourceActivationSnapshotRef | None = None
    model_call_id: str | None = None
    bindings_hash: str = field(init=False, repr=False, compare=False)
    activation_provenance_hash: str = field(
        init=False, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        _non_empty_string(
            self.activation_snapshot_id,
            "ResourceActivationSnapshotRef.activation_snapshot_id",
        )
        if self.snapshot_kind not in _SNAPSHOT_KINDS:
            raise _schema_error(
                f"未知 snapshot_kind: {self.snapshot_kind!r}，必须是 turn|model_call"
            )
        _non_empty_string(
            self.activation_policy_revision,
            "ResourceActivationSnapshotRef.activation_policy_revision",
        )
        validate_hash_token(
            self.activation_policy_hash,
            "ResourceActivationSnapshotRef.activation_policy_hash",
        )
        _non_negative_int(
            self.registry_generation,
            "ResourceActivationSnapshotRef.registry_generation",
        )
        for name in ("owner_session_id", "owner_thread_id", "turn_id", "captured_at"):
            _non_empty_string(
                getattr(self, name), f"ResourceActivationSnapshotRef.{name}"
            )
        if self.snapshot_kind == "turn":
            if self.parent is not None:
                raise ResourceActivationContractError(
                    "resource-activation-parent-invalid",
                    "turn snapshot 不得携带 parent turn snapshot",
                )
            if self.model_call_id is not None:
                raise ResourceActivationContractError(
                    "resource-activation-parent-invalid",
                    "turn snapshot 不得携带 model_call_id",
                )
        else:
            if not isinstance(self.parent, ResourceActivationSnapshotRef):
                raise ResourceActivationContractError(
                    "resource-activation-parent-invalid",
                    "model_call snapshot 必须携带 typed parent turn snapshot",
                )
            if self.parent.snapshot_kind != "turn":
                raise ResourceActivationContractError(
                    "resource-activation-parent-invalid",
                    "model_call snapshot 的 parent 必须是 turn snapshot",
                )
            if (
                self.parent.owner_session_id != self.owner_session_id
                or self.parent.owner_thread_id != self.owner_thread_id
                or self.parent.turn_id != self.turn_id
            ):
                raise ResourceActivationContractError(
                    "resource-activation-parent-invalid",
                    "model_call snapshot 的 owner/turn 必须与 parent 一致",
                )
            if not isinstance(self.model_call_id, str) or not self.model_call_id:
                raise ResourceActivationContractError(
                    "resource-activation-parent-invalid",
                    "model_call snapshot 必须携带非空 model_call_id",
                )
        ordered = _ordered_bindings(self.bindings)
        if not ordered:
            raise _schema_error(
                "ResourceActivationSnapshotRef 必须捕获至少一个 binding"
            )
        if self.snapshot_kind == "turn":
            if any(binding.effective_boundary != "turn" for binding in ordered):
                raise _schema_error("turn snapshot 只能包含 turn-bound binding")
        else:
            parent_bindings = self.parent.bindings
            if ordered[: len(parent_bindings)] != parent_bindings:
                raise ResourceActivationContractError(
                    "resource-activation-parent-invalid",
                    "model_call snapshot 必须逐字节复用 parent turn-bound binding 前缀",
                )
            if any(
                binding.effective_boundary == "turn"
                for binding in ordered[len(parent_bindings):]
            ):
                raise _schema_error(
                    "model_call snapshot 追加的 binding 必须使用 model_call boundary"
                )
            if self.registry_generation < self.parent.registry_generation:
                raise _schema_error(
                    "model_call snapshot.registry_generation 不得小于 parent"
                )
        object.__setattr__(self, "bindings", ordered)
        object.__setattr__(self, "bindings_hash", resource_bindings_hash(ordered))
        object.__setattr__(
            self,
            "activation_provenance_hash",
            resource_activation_provenance_hash(
                snapshot_kind=self.snapshot_kind,
                parent=self.parent,
                activation_policy_revision=self.activation_policy_revision,
                activation_policy_hash=self.activation_policy_hash,
                registry_generation=self.registry_generation,
                bindings=ordered,
            ),
        )

    @property
    def parent_turn_snapshot_id(self) -> str | None:
        """typed parent relation 的 id 投影；不进入任一内容 hash。"""

        return None if self.parent is None else self.parent.activation_snapshot_id

    def to_dict(self) -> dict[str, object]:
        return {
            "activation_snapshot_id": self.activation_snapshot_id,
            "snapshot_kind": self.snapshot_kind,
            "parent_turn_snapshot_id": self.parent_turn_snapshot_id,
            "activation_policy_revision": self.activation_policy_revision,
            "activation_policy_hash": self.activation_policy_hash,
            "registry_generation": self.registry_generation,
            "owner_session_id": self.owner_session_id,
            "owner_thread_id": self.owner_thread_id,
            "turn_id": self.turn_id,
            "model_call_id": self.model_call_id,
            "captured_at": self.captured_at,
            "bindings_hash": self.bindings_hash,
            "activation_provenance_hash": self.activation_provenance_hash,
            "bindings": [binding.to_dict() for binding in self.bindings],
        }

    @classmethod
    def from_dict(
        cls,
        value: object,
        *,
        parent: ResourceActivationSnapshotRef | None = None,
    ) -> ResourceActivationSnapshotRef:
        """恢复 snapshot；model_call 必须由调用方提供 typed parent relation。"""

        if not isinstance(value, Mapping):
            raise _schema_error("resource activation snapshot 必须是 typed object")
        _reject_forbidden_fields(
            value,
            allowed=RESOURCE_ACTIVATION_SNAPSHOT_FIELDS,
            aliases=SNAPSHOT_FIELD_ALIASES,
            where="ResourceActivationSnapshotRef",
        )
        missing = RESOURCE_ACTIVATION_SNAPSHOT_FIELDS - set(value)
        if missing:
            raise _schema_error(
                f"ResourceActivationSnapshotRef 缺少必填字段: {sorted(missing)}"
            )
        raw_bindings = value["bindings"]
        if not isinstance(raw_bindings, Sequence) or isinstance(
            raw_bindings, (str, bytes)
        ):
            raise _schema_error(
                "ResourceActivationSnapshotRef.bindings 必须是数组"
            )
        snapshot_kind = value["snapshot_kind"]
        declared_parent_id = value["parent_turn_snapshot_id"]
        if snapshot_kind == "model_call":
            if not isinstance(parent, ResourceActivationSnapshotRef):
                raise ResourceActivationContractError(
                    "resource-activation-parent-invalid",
                    "恢复 model_call snapshot 必须提供 typed parent turn snapshot",
                )
            if declared_parent_id != parent.activation_snapshot_id:
                raise ResourceActivationContractError(
                    "resource-activation-parent-invalid",
                    "parent_turn_snapshot_id 与提供的 parent relation 不一致",
                )
        elif declared_parent_id is not None:
            raise ResourceActivationContractError(
                "resource-activation-parent-invalid",
                "turn snapshot 的 parent_turn_snapshot_id 必须是 null",
            )
        restored = cls(
            activation_snapshot_id=value["activation_snapshot_id"],
            snapshot_kind=snapshot_kind,
            activation_policy_revision=value["activation_policy_revision"],
            activation_policy_hash=value["activation_policy_hash"],
            registry_generation=value["registry_generation"],
            owner_session_id=value["owner_session_id"],
            owner_thread_id=value["owner_thread_id"],
            turn_id=value["turn_id"],
            captured_at=value["captured_at"],
            bindings=tuple(
                ResourceProvenanceRef.from_dict(item) for item in raw_bindings
            ),
            parent=parent,
            model_call_id=value["model_call_id"],
        )
        for name in ("bindings_hash", "activation_provenance_hash"):
            if value[name] != getattr(restored, name):
                raise ResourceActivationContractError(
                    "resource-activation-hash-mismatch",
                    f"恢复的 {name} 与重算结果不一致",
                )
        return restored


__all__ = [
    "ABSOLUTE_PATH_FIELDS",
    "BOUNDARY_SINGULARITY_FIELDS",
    "CREDENTIAL_FIELDS",
    "LEGACY_FIELDS",
    "PROVENANCE_FIELD_ALIASES",
    "PROVIDER_LOCATOR_FIELDS",
    "RESOURCE_ACTIVATION_ERROR_CODES",
    "RESOURCE_ACTIVATION_SNAPSHOT_FIELDS",
    "RESOURCE_PROVENANCE_FIELDS",
    "SNAPSHOT_FIELD_ALIASES",
    "SOURCE_LINEAGE_REF_FIELDS",
    "ResourceActivationBoundary",
    "ResourceActivationContractError",
    "ResourceActivationSnapshotRef",
    "ResourceProvenanceRef",
    "SourceLineageRef",
    "resource_activation_provenance_hash",
    "resource_bindings_hash",
]
