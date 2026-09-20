"""冻结 Pydantic 与公开 Proto 的 Node Debug 字段集合。"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.schemas.internal_v2 import node_debug

PUBLIC_PROTO = Path.cwd() / "proto/boxteam/workspace/v2/public.proto"

PUBLIC_NODE_DEBUG_MODELS = (
    "ExtensionCatalogBindingAuditDTO",
    "NodeDebugActionRecordDTO",
    "NodeDebugActionRequest",
    "NodeDebugBreakpointDTO",
    "NodeDebugBreakpointRequest",
    "NodeDebugCapabilitiesDTO",
    "NodeDebugConfigurationActivateRequest",
    "NodeDebugConfigurationBreakpointDTO",
    "NodeDebugConfigurationCopyRequest",
    "NodeDebugConfigurationCreateRequest",
    "NodeDebugConfigurationDTO",
    "NodeDebugConfigurationImportRequest",
    "NodeDebugConfigurationSummaryDTO",
    "NodeDebugConfigurationUpdateRequest",
    "NodeDebugEvaluationDTO",
    "NodeDebugLaunchProfileDTO",
    "NodeDebugSessionManifestDTO",
    "NodeDebugStackFrameDTO",
    "NodeDebugStartRequest",
    "NodeDebugStateDTO",
    "NodeDebugVariableDTO",
)


def _message_fields(source: str, message_name: str) -> set[str]:
    match = re.search(
        rf"^message {re.escape(message_name)} \{{\n(?P<body>.*?)^\}}$",
        source,
        flags=re.MULTILINE | re.DOTALL,
    )
    if match is None:
        raise AssertionError(f"public.proto 缺少 {message_name}")
    return {
        field.group("name")
        for field in re.finditer(
            r"^\s*(?:optional\s+|repeated\s+)?[.\w<>]+\s+"
            r"(?P<name>[a-z][a-z0-9_]*)\s*=\s*\d+\s*;",
            match.group("body"),
            flags=re.MULTILINE,
        )
    }


@pytest.mark.parametrize("model_name", PUBLIC_NODE_DEBUG_MODELS)
def test_node_debug_pydantic_and_proto_fields_match(model_name: str) -> None:
    proto_source = PUBLIC_PROTO.read_text(encoding="utf-8")
    pydantic_fields = set(getattr(node_debug, model_name).model_fields)

    assert _message_fields(proto_source, model_name) == pydantic_fields
