"""v2 provider-neutral serialization and stable ordering helpers."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence

from app.domain.itemized.errors import ItemSchemaError
from app.domain.itemized.identity.refs import selection_ref_identity
from app.domain.itemized.redaction import validate_hash_redaction
from app.domain.itemized.validation import _non_empty_string

# 凭据字段必须先由 producer 显式脱敏；通用凭据与现有 HTTP header 名共用
# 字段级 marker 校验，大小写及连字符不构成绕过。工具 schema 属性是声明。
_CREDENTIAL_KEYS = frozenset({
    "token", "access_token", "refresh_token", "api_key", "apikey", "secret",
    "client_secret", "password", "credential", "credentials", "authorization",
    "auth", "proxy_authorization", "x_api_key", "x_goog_api_key", "cookie",
    "set_cookie",
})

# auth/transport/attempt/时间/请求 ID 载体只出现在 provider 请求参数层。正文、
# 业务 metadata 中的非凭据同名字段保留，实际凭据校验 marker；schema 声明
# 的属性名保持不变，否则不同语义会得到同一 hash。
_VOLATILE_REQUEST_KEYS = frozenset(
    {
        "authorization",
        "auth",
        "headers",
        "provider_request_id",
        "request_id",
        "response_id",
        "retry",
        "attempt",
        "created_at",
        "timestamp",
        "trace_id",
        "span_id",
        "transport",
    }
)

# wire 请求参数层携带 JSON Schema 声明的真实路径：只有这些位置决定“声明上下文”。
# 正文/业务 metadata 即使出现同形键，也只是普通值，不产生凭据或 volatile 豁免。
_REQUEST_TOOL_KEYS = frozenset({"tools"})
_REQUEST_STRUCTURED_OUTPUT_KEYS = frozenset({"response_format"})
_REQUEST_TEXT_KEYS = frozenset({"text"})
_TOOL_SCHEMA_KEYS = frozenset({"parameters", "input_schema"})

_MemberDispatch = Callable[[str, object], object]


def _string_key(key: object) -> str:
    if not isinstance(key, str):
        raise ItemSchemaError("hash preimage object key 必须是字符串")
    return key


def _project_declared(value: object) -> object:
    """声明子树：key 是属性/关键字名，不做凭据、volatile 或 marker 判定。"""
    if isinstance(value, Mapping):
        return {
            _string_key(key): _project_declared(child) for key, child in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_project_declared(child) for child in value]
    return value


def _project_members(
    value: Mapping[str, object],
    *,
    at_request: bool,
    dispatch: _MemberDispatch | None = None,
) -> dict[str, object]:
    """唯一的字段级投影：marker、凭据名与载体键规则只在这里实现一次。

    schema 声明上下文由 dispatch 显式给出；声明之外的所有字段（含载体包装层
    自身字段）都经过同一条凭据校验，不存在逐包装层的例外。
    """
    if "redaction_class" in value:
        return validate_hash_redaction(value)
    result: dict[str, object] = {}
    for key, child in value.items():
        name = _string_key(key)
        # 参数层的载体键先排除，因此其内部凭据不必再判定。
        if at_request and name.casefold() in _VOLATILE_REQUEST_KEYS:
            continue
        if name.casefold().replace("-", "_") in _CREDENTIAL_KEYS:
            if not isinstance(child, Mapping):
                raise ItemSchemaError(
                    "hash-redaction-required: 凭据必须使用 class/length/session digest"
                )
            result[name] = validate_hash_redaction(child)
            continue
        result[name] = dispatch(name, child) if dispatch is not None else _project_plain(child)
    return result


def _project_plain(value: object) -> object:
    """普通值：没有 wire 声明上下文。"""
    if isinstance(value, Mapping):
        return _project_members(value, at_request=False)
    if isinstance(value, (list, tuple)):
        return [_project_plain(child) for child in value]
    return value


def _is_function_tool(tool: Mapping[str, object]) -> bool:
    """只有 function 工具（Responses 可省略 type）携带参数 schema 声明。"""
    return tool.get("type", "function") == "function"


def _tool_member(name: str, child: object) -> object:
    """tools[] 项：仅 function 工具的参数 schema 进入声明上下文。"""
    if name in _TOOL_SCHEMA_KEYS and isinstance(child, Mapping):
        return _project_declared(child)
    if name == "function" and isinstance(child, Mapping):
        return _project_members(
            child, at_request=False, dispatch=_tool_function_member
        )
    return _project_plain(child)


def _tool_function_member(name: str, child: object) -> object:
    if name == "parameters" and isinstance(child, Mapping):
        return _project_declared(child)
    return _project_plain(child)


def _project_tool_definition(tool: Mapping[str, object]) -> dict[str, object]:
    if not _is_function_tool(tool):
        # 未知业务 type 不是 wire 声明载体，parameters 只能按普通值校验。
        return _project_members(tool, at_request=False)
    return _project_members(tool, at_request=False, dispatch=_tool_member)


def _structured_output_member(name: str, child: object) -> object:
    if name == "schema" and isinstance(child, Mapping):
        return _project_declared(child)
    if name == "json_schema" and isinstance(child, Mapping):
        return _project_members(
            child, at_request=False, dispatch=_json_schema_definition_member
        )
    return _project_plain(child)


def _json_schema_definition_member(name: str, child: object) -> object:
    if name == "schema" and isinstance(child, Mapping):
        return _project_declared(child)
    return _project_plain(child)


def _project_structured_output(value: Mapping[str, object]) -> dict[str, object]:
    """结构化输出载体：只有 type=json_schema 内的 schema 才是声明。"""
    if value.get("type") != "json_schema":
        return _project_members(value, at_request=False)
    return _project_members(value, at_request=False, dispatch=_structured_output_member)


def _text_member(name: str, child: object) -> object:
    if name == "format" and isinstance(child, Mapping):
        return _project_structured_output(child)
    return _project_plain(child)


def _project_text_carrier(value: Mapping[str, object]) -> dict[str, object]:
    """Responses 的 text 载体：format 才是结构化输出位置。"""
    return _project_members(value, at_request=False, dispatch=_text_member)


def _request_member(name: str, child: object) -> object:
    """请求参数层按 wire 路径分派；不在真实路径上的同形值保持普通语义。"""
    if name in _REQUEST_TOOL_KEYS:
        return project_declared_tools(child)
    if name in _REQUEST_STRUCTURED_OUTPUT_KEYS and isinstance(child, Mapping):
        return _project_structured_output(child)
    if name in _REQUEST_TEXT_KEYS and isinstance(child, Mapping):
        return _project_text_carrier(child)
    return _project_plain(child)


def project_declared_tools(value: object) -> object:
    """ToolSet 声明入口：调用方已用 typed 语义证明这是工具定义序列。"""
    if isinstance(value, (list, tuple)):
        return [
            _project_tool_definition(item) if isinstance(item, Mapping)
            else _project_plain(item)
            for item in value
        ]
    return _project_plain(value)


def _hash_safe_value(value: object) -> object:
    """消费 producer 预先生成的完整 marker，不创建会造成碰撞的统一占位符。"""
    return _project_plain(value)


def normalize_wire_request(value: object) -> object:
    """保留 request 语义，只剔除请求参数层的认证、传输和 attempt 载体。"""
    if isinstance(value, Mapping):
        return _project_members(value, at_request=True, dispatch=_request_member)
    return _project_plain(value)

def _contribution_order_key(value: object) -> tuple[int, str]:
    """返回唯一稳定的 request hash contribution 顺序。"""
    ordinal = getattr(value, "contribution_ordinal", None)
    if ordinal is None:
        # source_ordinal 只来自 itemized registry 分配的 typed 字段；自由
        # metadata 的同名键不再参与 request hash 排序。
        raw = getattr(value, "source_ordinal", None)
        if not isinstance(raw, int) or isinstance(raw, bool) or raw < 0:
            raise ItemSchemaError(
                "request hash contribution 缺少稳定 ordinal: "
                f"{getattr(value, 'contribution_id', '<unknown>')}"
            )
        ordinal = raw
    return (
        ordinal,
        _non_empty_string(
            getattr(value, "contribution_id", None),
            "contribution_id",
        ),
    )


def ordered_selection(entries: Sequence[object]) -> tuple[object, ...]:
    values = tuple(entries)
    ordinals = [getattr(value, "plan_ordinal", None) for value in values]
    if any(
        not isinstance(ordinal, int) or isinstance(ordinal, bool)
        for ordinal in ordinals
    ) or ordinals != list(range(len(values))):
        raise ItemSchemaError("selection.plan_ordinal 必须从 0 连续递增")
    # identity 口径统一由 selection_ref_identity 提供：ContextRef 使用
    # thread-qualified (ref_type, ref_id, thread_id)，不得在这里退回二元组。
    identities = [
        selection_ref_identity(getattr(value, "ref", None)) for value in values
    ]
    if len(identities) != len(set(identities)):
        raise ItemSchemaError("selection 不得重复引用同一个 ref")
    return values


__all__ = [
    "_contribution_order_key",
    "_hash_safe_value",
    "normalize_wire_request",
    "ordered_selection",
    "project_declared_tools",
]
