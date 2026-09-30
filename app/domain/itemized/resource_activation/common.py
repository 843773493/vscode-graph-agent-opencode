"""资源激活合同的共享基底：字段闭集、错误码、校验 helper 与 SourceLineageRef。

本模块是 resource_activation 子包内部共享定义的唯一归属处；provenance.py 与
snapshot.py 各自 import，禁止复制常量或 helper。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Final

from app.domain.itemized.enums import DetailAvailability
from app.domain.itemized.errors import ItemSchemaError
from app.domain.itemized.hashing import sha256_jcs


ResourceActivationBoundary = str
_BOUNDARIES: Final[frozenset[str]] = frozenset({"turn", "model_call"})
_SNAPSHOT_KINDS: Final[frozenset[str]] = _BOUNDARIES
_AVAILABILITIES: Final[frozenset[str]] = frozenset(
    item.value for item in DetailAvailability
)
_TOKEN_PATTERN_ERROR: Final = "必须匹配 ^[a-z][a-z0-9_-]{0,63}$"
_DISPLAY_URI_SCHEME: Final = "boxteam://"

# 每个 code 只在此定义一次；闭集与 snapshot/provenance 的 raise 点统一引用这些
# 符号，杜绝同一 code 在多处裸写导致的漂移。
_CODE_SCHEMA_INVALID: Final = "resource-activation-schema-invalid"
_CODE_BOUNDARY_SINGULARITY_REJECTED: Final = (
    "resource-activation-boundary-singularity-rejected"
)
_CODE_PROVIDER_LOCATOR_REJECTED: Final = "resource-activation-provider-locator-rejected"
_CODE_CREDENTIAL_REJECTED: Final = "resource-activation-credential-rejected"
_CODE_ABSOLUTE_PATH_REJECTED: Final = "resource-activation-absolute-path-rejected"
_CODE_LEGACY_FIELD_REJECTED: Final = "resource-activation-legacy-field-rejected"
_CODE_FIELD_ALIAS_REJECTED: Final = "resource-activation-field-alias-rejected"
_CODE_LINEAGE_INVALID: Final = "resource-activation-lineage-invalid"
_CODE_PARENT_INVALID: Final = "resource-activation-parent-invalid"
_CODE_ORDINAL_CONFLICT: Final = "resource-activation-ordinal-conflict"
_CODE_HASH_MISMATCH: Final = "resource-activation-hash-mismatch"

RESOURCE_ACTIVATION_ERROR_CODES: Final[frozenset[str]] = frozenset(
    {
        _CODE_SCHEMA_INVALID,
        _CODE_BOUNDARY_SINGULARITY_REJECTED,
        _CODE_PROVIDER_LOCATOR_REJECTED,
        _CODE_CREDENTIAL_REJECTED,
        _CODE_ABSOLUTE_PATH_REJECTED,
        _CODE_LEGACY_FIELD_REJECTED,
        _CODE_FIELD_ALIAS_REJECTED,
        _CODE_LINEAGE_INVALID,
        _CODE_PARENT_INVALID,
        _CODE_ORDINAL_CONFLICT,
        _CODE_HASH_MISMATCH,
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
    return ResourceActivationContractError(_CODE_SCHEMA_INVALID, message)


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
            _CODE_LEGACY_FIELD_REJECTED,
            f"{field_name} 是旧路径形态，正常 runtime 不得读取: {value!r}",
        )
    if value.startswith(("/", "\\")) or (
        len(value) >= 3 and value[1] == ":" and value[2] in "\\/"
    ):
        raise ResourceActivationContractError(
            _CODE_ABSOLUTE_PATH_REJECTED,
            f"{field_name} 不得是绝对路径: {value!r}",
        )
    if "\\" in value:
        raise ResourceActivationContractError(
            _CODE_ABSOLUTE_PATH_REJECTED,
            f"{field_name} 不得携带路径分隔符: {value!r}",
        )
    if value.find("://") > 0 and not value.startswith(_DISPLAY_URI_SCHEME):
        raise ResourceActivationContractError(
            _CODE_PROVIDER_LOCATOR_REJECTED,
            f"{field_name} 不得是 provider locator/endpoint: {value!r}",
        )
    if "@" in value:
        raise ResourceActivationContractError(
            _CODE_CREDENTIAL_REJECTED,
            f"{field_name} 不得携带 userinfo/credential 形态: {value!r}",
        )
    if "%" in value:
        raise _schema_error(f"{field_name} 拒绝百分号编码: {value!r}")


def _safe_identity(value: object, field_name: str) -> str:
    text = _non_empty_string(value, field_name)
    _reject_path_or_credential_shape(text, field_name)
    if any(marker in text for marker in ("/", "\\", "?", "#", " ")):
        raise ResourceActivationContractError(
            _CODE_PROVIDER_LOCATOR_REJECTED,
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
            _CODE_PROVIDER_LOCATOR_REJECTED,
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
                _CODE_BOUNDARY_SINGULARITY_REJECTED,
                f"{where} 不得有 assembly 单值 boundary 字段: {key!r}",
            )
        if key in CREDENTIAL_FIELDS:
            raise ResourceActivationContractError(
                _CODE_CREDENTIAL_REJECTED,
                f"{where} 不得携带 credential 字段: {key!r}",
            )
        if key in PROVIDER_LOCATOR_FIELDS:
            raise ResourceActivationContractError(
                _CODE_PROVIDER_LOCATOR_REJECTED,
                f"{where} 不得携带 provider locator/handle 字段: {key!r}",
            )
        if key in ABSOLUTE_PATH_FIELDS:
            raise ResourceActivationContractError(
                _CODE_ABSOLUTE_PATH_REJECTED,
                f"{where} 不得携带绝对路径/locator 字段: {key!r}",
            )
        if key in LEGACY_FIELDS:
            raise ResourceActivationContractError(
                _CODE_LEGACY_FIELD_REJECTED,
                f"{where} 不得携带旧字段: {key!r}",
            )
        if key in aliases:
            raise ResourceActivationContractError(
                _CODE_FIELD_ALIAS_REJECTED,
                f"{where} 不得使用别名字段 {key!r}，规范字段是 {aliases[key]!r}",
            )
        raise _schema_error(f"{where} 含未登记字段: {key!r}")


def _require_pair(value: object) -> tuple[object, object]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise ResourceActivationContractError(
            _CODE_LINEAGE_INVALID,
            "source_lineage_ref.sources 每条必须是 [source_id, revision]",
        )
    if len(value) != 2:
        raise ResourceActivationContractError(
            _CODE_LINEAGE_INVALID,
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
                _CODE_LINEAGE_INVALID,
                "SourceLineageRef.sources 必须是元组",
            )
        seen: set[str] = set()
        for entry in self.sources:
            if not isinstance(entry, tuple) or len(entry) != 2:
                raise ResourceActivationContractError(
                    _CODE_LINEAGE_INVALID,
                    "SourceLineageRef.sources 必须是 (source_id, revision) 元组",
                )
            source_id = _safe_identity(entry[0], "SourceLineageRef.source_id")
            _non_empty_string(entry[1], "SourceLineageRef.revision")
            if source_id in seen:
                raise ResourceActivationContractError(
                    _CODE_LINEAGE_INVALID,
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
                _CODE_HASH_MISMATCH,
                "source_lineage_ref.digest 与来源向量/派生版本不一致",
            )
        return lineage
