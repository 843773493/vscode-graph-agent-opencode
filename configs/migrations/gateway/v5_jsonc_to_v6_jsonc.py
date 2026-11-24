"""将 Gateway v5 平面覆盖层转换为 v6 显式父节点信封。"""

from __future__ import annotations


def migrate_v5_jsonc_to_v6_jsonc(
    payload: dict[str, object], *, source_key: str, user_layer_present: bool,
) -> dict[str, object]:
    if payload.get("config_version") != 5:
        raise ValueError("Gateway v5 转换只接受 config_version=5 的平面来源")
    parents = {
        "gateway.jsonc": "builtin:gateway",
        "gateway_mutable_override": "builtin:gateway",
        "gateway_local.jsonc": "user:gateway" if user_layer_present else "builtin:gateway",
        "gateway_local_mutable_override": "user:gateway" if user_layer_present else "builtin:gateway",
    }
    if source_key not in parents:
        raise ValueError(f"Gateway v5 来源层未知，保留原件: {source_key}")
    business = {key: value for key, value in payload.items() if key not in {"$schema", "config_version"}}
    return {
        **({"$schema": payload["$schema"]} if "$schema" in payload else {}),
        "config_version": 6,
        "extends": parents[source_key],
        "settings": {"gateway": business},
    }
