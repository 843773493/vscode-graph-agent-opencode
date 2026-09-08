"""draft registry 的凭据拒绝边界；不改写 manifest，也不生成脱敏占位符。"""

from __future__ import annotations

from collections.abc import Mapping

from app.domain.itemized.redaction import validate_hash_redaction
from app.domain.itemized.request_plan import ContextRequestPlan
from app.domain.itemized.serialization import _hash_safe_value

_HTTP_CREDENTIAL_KEYS = frozenset(
    {
        "authorization",
        "proxy-authorization",
        "x-api-key",
        "api-key",
        "cookie",
        "set-cookie",
    }
)


def _validate_http_credentials(value: object) -> None:
    """补充 domain 之外的明确 HTTP 凭据名，不扫描正文或猜测未知字段。"""
    if isinstance(value, Mapping):
        for key, child in value.items():
            if not isinstance(key, str):
                raise TypeError("registry metadata/policy key 必须是字符串")
            if key.lower() in _HTTP_CREDENTIAL_KEYS:
                if not isinstance(child, Mapping):
                    raise ValueError("HTTP 凭据必须是完整脱敏 marker")
                validate_hash_redaction(child)
            else:
                _validate_http_credentials(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            _validate_http_credentials(child)


def _validate_metadata(value: object, *, field: str) -> None:
    try:
        if not isinstance(value, Mapping):
            raise TypeError("registry metadata/policy 必须是 object")
        # domain 负责唯一的凭据键规则和 marker schema；返回值仅校验，不写回。
        _hash_safe_value(value)
        _validate_http_credentials(value)
    except Exception:  # noqa: BLE001 - 嵌套值或自定义 mapping 的异常可能包含凭据
        failed = True
    else:
        failed = False
    if failed:
        # except 外重抛，避免 cause/context 保留原值；field 只能由下方固定调用点提供。
        raise ValueError(
            f"plan-privacy-required: {field} 含未脱敏凭据或非法 metadata/marker"
        )


def validate_draft_privacy(plan: ContextRequestPlan) -> None:
    """写前/恢复前共用的纯校验；拒绝原始凭据，保留所有输入及其哈希含义。

    contribution body 的写前剥离、恢复拒绝及 typed owner 校验由 registry
    owner 负责。本函数不扫描正文或工具 JSON Schema，也不以字段名猜测任意
    文本的敏感度；producer 必须显式标记其余敏感值。
    """
    if not isinstance(plan, ContextRequestPlan):
        raise TypeError("validate_draft_privacy 必须接收 ContextRequestPlan")
    for contribution in plan.contributions:
        _validate_metadata(contribution.metadata, field="contribution.metadata")
    for tool_ref in plan.tool_set_refs:
        _validate_metadata(tool_ref.tool_policy, field="tool_policy")


__all__ = ["validate_draft_privacy"]
