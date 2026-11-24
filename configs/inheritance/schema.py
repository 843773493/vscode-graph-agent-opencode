"""配置来源的节点信封；解析与原来源编辑器复用同一 JSON Schema。"""

from __future__ import annotations

from typing import Literal

from configs.network import NetworkSettings


def config_document_schema(
    *, domain: Literal["gateway", "workspace"], version: int,
) -> dict[str, object]:
    """只校验本地定义；完整业务值由合并后的所属域 schema 校验。"""
    network = NetworkSettings.model_json_schema()
    # network 可只写局部定义；proxy 是完整互斥块，保留其内部必需字段。
    network.pop("required")
    definitions = network.pop("$defs")
    for name in ("EnvironmentProxy", "DirectProxy", "ExplicitProxy"):
        definitions[name]["required"] = ["mode", *definitions[name].get("required", [])]
    definitions["networkSettings"] = network
    reference = {"type": "string", "minLength": 1}
    common = {"network": {"$ref": "#/$defs/networkSettings"}}
    if domain == "workspace":
        common["model_options"] = {"type": "object"}
    definitions["profileSettings"] = {
        "type": "object", "additionalProperties": False, "properties": common,
    }
    definitions["scopeSettings"] = {
        "type": "object", "additionalProperties": False,
        "properties": {**common, domain: {"type": "object"}},
    }
    definitions["configNode"] = {
        "type": "object", "additionalProperties": False, "required": ["settings"],
        "properties": {"extends": reference, "settings": {"$ref": "#/$defs/profileSettings"}},
    }
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object", "additionalProperties": False,
        "required": ["config_version", "settings"], "$defs": definitions,
        "properties": {
            "$schema": {"type": "string"},
            "config_version": {"type": "integer", "const": version},
            "extends": reference,
            "settings": {"$ref": "#/$defs/scopeSettings"},
            "profiles": {
                "type": "object", "propertyNames": {"minLength": 1},
                "additionalProperties": {"$ref": "#/$defs/configNode"},
            },
        },
    }
