from __future__ import annotations

import logging
from typing import cast

from app.core.config_sources import (
    ConfigSource,
    ConfigSourceLayer,
    ConfigSourcePresence,
    validate_config_source_uri,
)
from app.core.failures import failure_detail
from app.gateway.control.gateway_state import GatewayStateStore

logger = logging.getLogger(__name__)


def record_gateway_restart_startup_failure(
    *,
    state_store: GatewayStateStore,
    candidate_ref: str,
    error: BaseException,
    gateway_id: str,
    target_generation: str,
    fencing_token: str,
) -> None:
    """在 Gateway 配置解析尚未完成时，把 owned pending 固定到恢复态。"""

    if not isinstance(error, BaseException):
        raise TypeError("Gateway 启动失败必须提供原异常")
    state_store.record_gateway_restart_startup_failure(
        candidate_ref=candidate_ref,
        gateway_id=gateway_id,
        target_generation=target_generation,
        fencing_token=fencing_token,
        error=failure_detail(error, stage="startup", label="Gateway pending generation 启动失败"),
    )


def _gateway_persisted_source_details(
    source_baseline: dict[str, object],
) -> tuple[ConfigSource, ...]:
    """从 active/pending baseline 恢复逻辑来源，不把快照本身当作来源。"""

    if not source_baseline:
        raise ValueError("Gateway active/pending snapshot 来源基线不能为空")
    details: list[ConfigSource] = []
    for source_key, raw_value in sorted(source_baseline.items()):
        if not isinstance(raw_value, dict):
            raise TypeError(f"Gateway source baseline 必须是对象: key={source_key}")
        raw_value = cast(dict[str, object], raw_value)
        raw_uri = raw_value.get("uri")
        if raw_uri is not None and (not isinstance(raw_uri, str) or not raw_uri):
            raise ValueError(
                f"Gateway source baseline uri 必须是非空字符串或 None: key={source_key}"
            )
        validate_config_source_uri(raw_uri)
        presence_value = raw_value.get("presence")
        if presence_value == "present":
            presence: ConfigSourcePresence = "present"
        elif presence_value == "absent":
            presence = "absent"
        else:
            raise ValueError(
                f"Gateway source baseline presence 无效: key={source_key}"
            )
        if raw_value.get("layer") not in {"inline", "user", "user_local", "source"}:
            raise ValueError(f"Gateway source baseline layer 无效: key={source_key}")
        if type(raw_value.get("precedence")) is not int or raw_value["precedence"] < 0:
            raise ValueError(f"Gateway source baseline precedence 无效: key={source_key}")
        if not isinstance(raw_value.get("document"), dict):
            raise TypeError(f"Gateway source baseline 缺少固定来源定义: key={source_key}")
        layer = cast(ConfigSourceLayer, raw_value["layer"])
        precedence = raw_value["precedence"]
        details.append(
            ConfigSource(
                uri=raw_uri,
                layer=layer,
                precedence=precedence,
                loaded=presence == "present",
                source_key=source_key,
                presence=presence,
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
    details.sort(key=lambda source: source.precedence)
    if [source.precedence for source in details] != list(range(len(details))):
        raise ValueError("Gateway source baseline precedence 必须是连续的唯一父链顺序")
    return tuple(details)
