from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class SessionGenerationProviderProtocol(Protocol):
    type_id: str
    supported_versions: frozenset[str]
    config_schema: dict[str, object]

    def build_prompt(self, config: dict[str, object]) -> str: ...


class AgentPromptGenerationProvider:
    type_id = "builtin.agent_prompt"
    supported_versions = frozenset({"1"})
    config_schema: dict[str, object] = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "properties": {
            "prompt": {"type": "string", "minLength": 1},
            "session_title": {"type": "string", "minLength": 1},
        },
        "required": ["prompt"],
        "additionalProperties": False,
    }

    def build_prompt(self, config: dict[str, object]) -> str:
        prompt = config.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("builtin.agent_prompt 生成器配置缺少非空 prompt")
        return prompt.strip()
