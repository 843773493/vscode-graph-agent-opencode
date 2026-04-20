from __future__ import annotations
import json
import os
from typing import Optional

import jsonschema
from jsonschema import ValidationError

from app.schemas.config import ConfigDTO, ConfigUpdateRequest

CONFIGS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "configs")

_config_path_override: Optional[str] = None


def set_config_path(config_path: str) -> None:
    global _config_path_override
    _config_path_override = config_path


def get_config_path() -> Optional[str]:
    return _config_path_override


class ConfigService:
    _instance: Optional[ConfigService] = None

    def __init__(self):
        self._boxteam_config: Optional[dict] = None
        self._schema: Optional[dict] = None

    @classmethod
    def get_instance(cls) -> "ConfigService":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @classmethod
    def reset_instance(cls) -> None:
        cls._instance = None

    def _load_schema(self) -> dict:
        if self._schema is None:
            schema_path = os.path.join(CONFIGS_DIR, "config.json")
            with open(schema_path, "r", encoding="utf-8") as f:
                self._schema = json.load(f)
        return self._schema

    def _get_boxteam_config_path(self) -> Optional[str]:
        if _config_path_override:
            return _config_path_override
        return os.path.join(CONFIGS_DIR, "boxteam.json")

    def _load_boxteam_config(self) -> dict:
        if self._boxteam_config is None:
            config_path = self._get_boxteam_config_path()
            if config_path and os.path.exists(config_path):
                with open(config_path, "r", encoding="utf-8") as f:
                    self._boxteam_config = json.load(f)
            else:
                self._boxteam_config = {}
        return self._boxteam_config

    def _apply_workspace_override(self, workspace_root: str) -> None:
        if self._boxteam_config is None:
            return
        override_path = os.path.join(workspace_root, ".boxteam", "boxteam.json")
        if os.path.exists(override_path):
            with open(override_path, "r", encoding="utf-8") as f:
                override_config = json.load(f)
            self._boxteam_config = self._merge_config(self._boxteam_config, override_config)

    def _merge_config(self, base: dict, override: dict) -> dict:
        result = base.copy()
        for key, value in override.items():
            if key in result and isinstance(result[key], dict) and isinstance(value, dict):
                result[key] = self._merge_config(result[key], value)
            else:
                result[key] = value
        return result

    def validate_boxteam_config(self) -> None:
        schema = self._load_schema()
        config = self._load_boxteam_config()
        jsonschema.validate(config, schema)

    def get_llm_providers(self) -> list[dict]:
        config = self._load_boxteam_config()
        return config.get("llm", {}).get("providers", [])

    async def get(self) -> ConfigDTO:
        return ConfigDTO(
            default_model="gpt-4.1",
            default_orchestration="hierarchical",
            max_concurrent_agents=4,
            allow_shell_tools=False,
            ignored_paths=[".git", "node_modules", "__pycache__", ".venv"],
            auto_summarize=True,
            metadata={
                "version": "1.0.0",
                "environment": "development"
            }
        )

    async def update(self, update_request: ConfigUpdateRequest) -> ConfigDTO:
        current = await self.get()
        update_data = update_request.model_dump(exclude_unset=True)
        return ConfigDTO(**{**current.model_dump(), **update_data})
