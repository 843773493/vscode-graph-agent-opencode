from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal
from uuid import uuid4

ConfigLifecycleState = Literal[
    "none",
    "candidate_validated",
    "pending_restart",
    "applying",
    "active",
    "rejected",
    "discarded",
    "conflict",
    "recovery_required",
]

ConfigResult = Literal[
    "applied",
    "restart_required",
    "restart_failed",
    "apply_failed",
    "rejected",
    "conflict",
    "discarded",
    "recovery_required",
    "unchanged",
]

ConfigEventRelayState = Literal["pending", "claimed", "delivered", "failed"]

ConfigActivationScope = Literal[
    "current",
    "next_job",
    "next_session",
    "restart_workspace",
    "restart_gateway",
    "mixed",
    "unknown",
]

CONFIG_LIFECYCLE_STATES: frozenset[str] = frozenset(
    {
        "none",
        "candidate_validated",
        "pending_restart",
        "applying",
        "active",
        "rejected",
        "discarded",
        "conflict",
        "recovery_required",
    }
)

_VALID_STATE_TRANSITIONS: dict[str, frozenset[str]] = {
    "none": frozenset({"candidate_validated", "rejected", "conflict"}),
    "candidate_validated": frozenset(
        {"applying", "pending_restart", "rejected", "conflict", "discarded"}
    ),
    "pending_restart": frozenset(
        {"applying", "pending_restart", "conflict", "discarded", "recovery_required"}
    ),
    "applying": frozenset(
        {"active", "pending_restart", "rejected", "conflict", "recovery_required"}
    ),
    "active": frozenset({"candidate_validated", "active", "conflict"}),
    "rejected": frozenset({"candidate_validated"}),
    "discarded": frozenset(),
    "conflict": frozenset({"candidate_validated", "discarded", "recovery_required"}),
    "recovery_required": frozenset(
        {
            "candidate_validated",
            "applying",
            "active",
            "pending_restart",
            "discarded",
            "recovery_required",
        }
    ),
}


class ConfigStateTransitionError(ValueError):
    """配置状态转换不符合持久化状态机。"""


class ConfigConflictError(RuntimeError):
    """配置来源或 active 基线已经变化，拒绝覆盖较新的记录。"""


class ConfigEventCursorGoneError(RuntimeError):
    """请求的配置事件游标已不在保留窗口内，调用方必须重新读取 snapshot。"""

    def __init__(self, *, config_domain: str, after: int, first: int) -> None:
        super().__init__(
            "配置事件游标已过期: "
            f"domain={config_domain}, after={after}, first_available={first}"
        )
        self.config_domain = config_domain
        self.after = after
        self.first_available = first


class SecretReferenceRequiredError(ValueError):
    """旧配置中的秘密无法安全恢复（例如不可逆摘要），必须先重新导入。"""

    code = "secret_reference_required"


class SecretResolutionError(ValueError):
    """持久化的 secret reference 在当前运行时无法解析。"""

    code = "secret_resolution_failed"


def validate_state_transition(
    current: ConfigLifecycleState,
    target: ConfigLifecycleState,
) -> None:
    if current not in CONFIG_LIFECYCLE_STATES:
        raise ConfigStateTransitionError(f"未知配置状态: {current}")
    if target not in CONFIG_LIFECYCLE_STATES:
        raise ConfigStateTransitionError(f"未知配置状态: {target}")
    if target not in _VALID_STATE_TRANSITIONS[current]:
        raise ConfigStateTransitionError(
            f"非法配置状态转换: {current} -> {target}"
        )


def dump_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def load_json_object(value: str, *, field: str) -> dict[str, object]:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as error:
        raise ValueError(f"{field} JSON 无效: {error}") from error
    if not isinstance(parsed, dict):
        raise TypeError(f"{field} 必须是 JSON 对象")
    return parsed


def _json_pointer_escape(value: str) -> str:
    return value.replace("~", "~0").replace("/", "~1")


def changed_json_paths(
    previous: object,
    current: object,
    *,
    array_identity_keys: dict[str, str] | None = None,
) -> tuple[str, ...]:
    """返回两个 JSON 值之间的精确 JSON Pointer 路径。"""

    paths: list[str] = []

    def pointer(path: tuple[str, ...]) -> str:
        return "/" + "/".join(_json_pointer_escape(part) for part in path)

    def visit(left: object, right: object, path: tuple[str, ...]) -> None:
        if isinstance(left, dict) and isinstance(right, dict):
            for key in sorted(set(left) | set(right), key=str):
                key_text = str(key)
                if key not in left or key not in right:
                    paths.append(pointer((*path, key_text)))
                else:
                    visit(left[key], right[key], (*path, key_text))
            return
        if isinstance(left, list) and isinstance(right, list):
            if left == right:
                return
            identity_key = (array_identity_keys or {}).get(pointer(path))
            if identity_key is None:
                paths.append(pointer(path))
                return
            left_by_id = _index_json_array(left, identity_key)
            right_by_id = _index_json_array(right, identity_key)
            if left_by_id is None or right_by_id is None:
                paths.append(pointer(path))
                return
            if [str(item[identity_key]) for item in left] != [
                str(item[identity_key]) for item in right
            ]:
                paths.append(pointer(path))
                return
            for identity in sorted(set(left_by_id) | set(right_by_id)):
                item_path = (*path, identity)
                if identity not in left_by_id or identity not in right_by_id:
                    paths.append(pointer(item_path))
                    continue
                visit(left_by_id[identity], right_by_id[identity], item_path)
            return
        if left != right:
            paths.append(pointer(path))

    visit(previous, current, ())
    return tuple(paths)


def _index_json_array(value: list[object], identity_key: str) -> dict[str, object] | None:
    indexed: dict[str, object] = {}
    for item in value:
        if not isinstance(item, dict) or identity_key not in item:
            return None
        identity = str(item[identity_key])
        if not identity or identity in indexed:
            return None
        indexed[identity] = item
    return indexed


def new_config_id(prefix: str) -> str:
    if not prefix or not prefix.replace("_", "").isalnum():
        raise ValueError("配置 ID 前缀必须是非空字母数字字符串")
    return f"{prefix}_{uuid4().hex}"


_SECRET_FIELD_NAMES = frozenset(
    {
        "api_key",
        "access_token",
        "client_secret",
        "password",
        "secret",
        "token",
    }
)

_ENV_SECRET_PATTERN = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$")

# 字面量秘密在"诊断/事件/日志"侧统一表示为不可逆摘要；持久化侧则保留原文。
_LITERAL_SECRET_PREFIX = "literal-sha256:"


def _environment_secret_name(value: str) -> str | None:
    match = _ENV_SECRET_PATTERN.fullmatch(value)
    return match.group(1) if match is not None else None


def normalize_secret_reference(value: str) -> str:
    env_name = _environment_secret_name(value)
    if env_name is not None:
        return f"env:{env_name}"
    if value.startswith("env:") and value[4:]:
        return value
    if value.startswith(_LITERAL_SECRET_PREFIX):
        return value
    return f"{_LITERAL_SECRET_PREFIX}{hashlib.sha256(value.encode('utf-8')).hexdigest()}"


def _literal_secret_digest(value: str) -> str:
    """返回字面量秘密的不可逆摘要，只用于诊断、事件和日志。"""
    return f"{_LITERAL_SECRET_PREFIX}{hashlib.sha256(value.encode('utf-8')).hexdigest()}"


def migrate_legacy_secret_payload(
    value: object,
) -> tuple[dict[str, object], tuple[str, ...]]:
    """升级旧持久化 payload。

    - ``${ENV_NAME}`` 规范化为 ``env:NAME`` 引用；
    - 字面量 key 保留原文（现在受支持），可正常重启恢复；
    - ``literal-sha256:`` 是旧版本写入的不可逆摘要，无法还原出密钥，必须
      返回阻断路径交给调用方建立恢复态，不能当作可用密钥静默放行。
    """

    blocked_paths: list[str] = []

    def visit(current: object, path: tuple[str, ...]) -> object:
        if isinstance(current, dict):
            result: dict[str, object] = {}
            for key, item in current.items():
                key_text = str(key)
                item_path = (*path, key_text)
                if key_text in _SECRET_FIELD_NAMES and isinstance(item, str):
                    if item.startswith(_LITERAL_SECRET_PREFIX):
                        blocked_paths.append("/" + "/".join(item_path))
                        result[key_text] = item
                        continue
                    reference = normalize_secret_reference(item)
                    result[key_text] = (
                        reference if reference.startswith("env:") else item
                    )
                else:
                    result[key_text] = visit(item, item_path)
            return result
        if isinstance(current, list):
            return [
                visit(item, (*path, str(index)))
                for index, item in enumerate(current)
            ]
        return current

    migrated = visit(value, ())
    if not isinstance(migrated, dict):
        raise TypeError("旧配置秘密迁移要求对象 payload")
    return migrated, tuple(sorted(blocked_paths))


def _resolve_secret_reference(reference: str, *, path: str) -> str:
    """解析 ``env:`` 引用；字面量自包含，不需要也不能从摘要解析回原文。"""
    if not reference.startswith("env:") or not reference[4:]:
        raise SecretReferenceRequiredError(
            f"配置秘密引用不受支持: path={path}, reference={reference}"
        )
    env_name = reference[4:]
    resolved = os.environ.get(env_name)
    if resolved is None:
        raise SecretResolutionError(
            f"配置秘密引用无法解析: path={path}, reference=env:{env_name}"
        )
    return resolved


def prepare_config_for_persistence(
    value: object,
    *,
    resolve_environment: bool = False,
) -> dict[str, object]:
    """生成可持久化的候选 payload。

    支持两种秘密输入：``${ENV_NAME}``（规范化为 ``env:NAME`` 引用）和字面量
    key（例如本地部署模型的 dummy key、临时测试 key）。字面量按原文写入
    SQLite，保证重启后能原样恢复；而诊断、事件和日志侧由
    ``redact_config_payload``/``build_secret_binding_summary`` 统一降级为
    不可逆摘要，二者职责分离。
    """

    def visit(current: object, path: tuple[str, ...]) -> object:
        if isinstance(current, dict):
            result: dict[str, object] = {}
            for key, item in current.items():
                key_text = str(key)
                item_path = (*path, key_text)
                if key_text in _SECRET_FIELD_NAMES and isinstance(item, str):
                    reference = normalize_secret_reference(item)
                    # 字面量自包含：无需也不能解析环境变量。
                    if resolve_environment and reference.startswith("env:"):
                        _resolve_secret_reference(
                            reference,
                            path="/" + "/".join(item_path),
                        )
                    # env 引用存引用本身；字面量存原文，才能原样重启恢复。
                    result[key_text] = (
                        reference
                        if reference.startswith("env:")
                        else item
                    )
                else:
                    result[key_text] = visit(item, item_path)
            return result
        if isinstance(current, list):
            return [visit(item, (*path, str(index))) for index, item in enumerate(current)]
        return current

    prepared = visit(value, ())
    if not isinstance(prepared, dict):
        raise TypeError("持久化配置 payload 必须是对象")
    return prepared


def redact_config_payload(value: object) -> object:
    """生成可对外展示的脱敏副本。

    秘密字段一律替换为引用或不可逆摘要：``env:NAME`` 保留引用，字面量
    降级为 ``literal-sha256:<digest>``。这个函数只用于诊断、事件、日志和
    备份，不参与持久化，因此与 ``prepare_config_for_persistence`` 职责分离。
    """

    if isinstance(value, dict):
        return {
            str(key): (
                (
                    _literal_secret_digest(item)
                    if not item.startswith(("env:", _LITERAL_SECRET_PREFIX))
                    else normalize_secret_reference(item)
                )
                if str(key) in _SECRET_FIELD_NAMES and isinstance(item, str)
                else redact_config_payload(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_config_payload(item) for item in value]
    return value


def restore_environment_secret_references(value: object) -> object:
    """把持久化 payload 还原为用户配置契约。

    ``env:NAME`` 还原为 ``${NAME}``；字面量 key 原样返回，因为它在持久化时
    就按原文保存，可以完整往返。``literal-sha256:`` 是不可逆摘要，无法还原，
    必须显式报错而不是把摘要当作密钥使用。
    """

    if isinstance(value, dict):
        restored: dict[str, object] = {}
        for key, item in value.items():
            key_text = str(key)
            if key_text in _SECRET_FIELD_NAMES and isinstance(item, str):
                if item.startswith("env:") and item[4:]:
                    restored[key_text] = "${" + item[4:] + "}"
                    continue
                if item.startswith(_LITERAL_SECRET_PREFIX):
                    raise ValueError(
                        f"无法从 secret_ref 恢复 literal secret: path={key_text}"
                    )
            restored[key_text] = restore_environment_secret_references(item)
        return restored
    if isinstance(value, list):
        return [restore_environment_secret_references(item) for item in value]
    return value


def build_secret_binding_summary(
    value: object,
    *,
    resolve_environment: bool = False,
) -> dict[str, object]:
    """返回引用和绑定摘要；永远不把解析后的秘密写入结果。

    ``resolve_environment`` 只决定 env 引用是否用解析后的值计算绑定的
    digest（用于检测密钥轮换）。字面量自包含，直接按其原文计算。
    """

    bindings: dict[str, object] = {}

    def visit(current: object, path: tuple[str, ...]) -> None:
        if isinstance(current, dict):
            for key, item in current.items():
                key_text = str(key)
                item_path = (*path, key_text)
                if key_text in _SECRET_FIELD_NAMES and isinstance(item, str):
                    reference = normalize_secret_reference(item)
                    if reference.startswith("env:"):
                        material = (
                            _resolve_secret_reference(
                                reference,
                                path="/" + "/".join(item_path),
                            )
                            if resolve_environment
                            else reference
                        )
                    else:
                        material = item
                    binding_digest = hashlib.sha256(
                        material.encode("utf-8")
                    ).hexdigest()
                    bindings["/" + "/".join(item_path)] = {
                        "secret_ref": reference,
                        "secret_version": binding_digest[:16],
                        "binding_digest": binding_digest,
                    }
                else:
                    visit(item, item_path)
        elif isinstance(current, list):
            for index, item in enumerate(current):
                visit(item, (*path, str(index)))

    visit(value, ())
    return bindings


@dataclass(frozen=True, slots=True)
class ConfigSourceLayerRecord:
    config_key: str
    source_path: str
    presence: Literal["present", "absent"]
    config_version: int
    payload: dict[str, object] | None
    layer_revision: int
    layer_digest: str | None
    source_generation: int
    previous_digest: str | None
    updated_at: datetime
    previous_payload: dict[str, object] | None = None
    backup_path: str | None = None


@dataclass(frozen=True, slots=True)
class ConfigActiveSnapshotRecord:
    config_domain: str
    active_revision: int
    candidate_id: str | None
    payload: dict[str, object]
    source_baseline: dict[str, object]
    source_generation: int
    layer_revisions: dict[str, int]
    layer_digests: dict[str, str | None]
    effective_digest: str
    secret_bindings: dict[str, object]
    schema_version: int
    promoted_generation: str | None
    promoted_apply_id: str | None
    promoted_at: datetime
    state: ConfigLifecycleState = "active"
    last_error: str | None = None


@dataclass(frozen=True, slots=True)
class ConfigPendingCandidateRecord:
    config_domain: str
    candidate_id: str
    idempotency_key: str
    pending_revision: int
    payload: dict[str, object]
    source_baseline: dict[str, object]
    candidate_digest: str
    effective_digest: str
    target_generation: str | None
    fencing_token: str | None
    state: ConfigLifecycleState
    last_error: str | None
    created_at: datetime
    base_active_revision: int | None = None
    persistence_location: str | None = None
    source_generation: int | None = None
    last_attempt_id: str | None = None
    last_apply_id: str | None = None
    candidate_ref: str | None = None
    secret_bindings: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ConfigEventRecord:
    event_seq: int
    event_id: str
    config_domain: str
    candidate_id: str | None
    attempt_id: str | None
    apply_id: str | None
    idempotency_key: str | None
    commit_revision: int | None
    active_revision: int | None
    pending_revision: int | None
    source: str
    result: ConfigResult
    activation_scope: ConfigActivationScope
    changed_paths: tuple[str, ...]
    applied_paths: tuple[str, ...]
    deferred_paths: tuple[str, ...]
    error: str | None
    occurred_at: datetime
    relay_state: ConfigEventRelayState = "pending"
    relay_attempts: int = 0
    relay_last_error: str | None = None
    relay_claimed_by: str | None = None
    relay_claimed_until: datetime | None = None
    relay_next_attempt_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class ConfigEventInput:
    """在配置状态事务中一并写入的 outbox 事件。"""

    event_id: str
    config_domain: str
    candidate_id: str | None
    attempt_id: str | None
    apply_id: str | None
    idempotency_key: str | None
    commit_revision: int | None
    active_revision: int | None
    pending_revision: int | None
    source: str
    result: ConfigResult
    activation_scope: ConfigActivationScope = "unknown"
    changed_paths: tuple[str, ...] = ()
    applied_paths: tuple[str, ...] = ()
    deferred_paths: tuple[str, ...] = ()
    error: str | None = None


@dataclass(frozen=True, slots=True)
class ConfigApplyClaimRecord:
    """外部 apply 的租约和 fencing token。"""

    config_domain: str
    candidate_id: str
    attempt_id: str
    apply_id: str
    owner: str
    base_active_revision: int | None
    target_generation: str | None
    lease_expires_at: datetime
    fencing_token: str
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class ConfigApplyJournalRecord:
    """记录外部副作用应用的 durable prepare/apply/commit 边界。"""

    config_domain: str
    apply_id: str
    candidate_id: str
    attempt_id: str
    owner: str
    base_active_revision: int | None
    pending_revision: int | None
    source_baseline: dict[str, object]
    active_baseline: dict[str, object]
    registry_revision: int | None
    side_effects: tuple[dict[str, object], ...]
    state: Literal[
        "applying",
        "committed",
        "failed",
        "recovery_required",
        "compensated",
    ]
    last_error: str | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class ConfigSourceJournalRecord:
    """共享 source owner 分配的不可复用 source generation。"""

    source_key: str
    source_generation: int
    source_event_id: str
    source_path: str
    presence: Literal["present", "absent"]
    layer_revision: int
    layer_digest: str | None
    previous_digest: str | None
    origin: str
    fanout_id: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class GatewayRestartIntentRecord:
    intent_id: str
    candidate_ref: str
    candidate_id: str
    gateway_id: str | None
    base_active_revision: int | None
    old_generation: str | None
    target_generation: str
    fencing_token: str
    state: Literal[
        "pending",
        "applying",
        "active",
        "failed",
        "recovery_required",
        "discarded",
    ]
    requested_by: str
    health_proof: dict[str, object] | None
    last_error: str | None
    requested_at: datetime
    expires_at: datetime | None
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class GatewayRuntimeGenerationRecord:
    config_domain: str
    generation_id: str
    process_id: int | None
    loaded_source: Literal["active", "pending"]
    candidate_id: str | None
    active_revision: int | None
    pending_revision: int | None
    candidate_digest: str | None
    effective_digest: str
    secret_binding_digest: str | None
    fencing_token: str | None
    listener_state: Literal["reserved", "serving", "draining", "closed"]
    state: Literal["starting", "healthy", "active", "failed", "closed"]
    health_proof: dict[str, object] | None
    created_at: datetime
    updated_at: datetime
