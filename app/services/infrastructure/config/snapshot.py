from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, cast

from app.core.config_sources import ConfigSource
from app.runtime.network_proxy import ProxyRoutes
from configs.inheritance import ConfigGraph
from configs.inheritance.values import ConfigValue, freeze, thaw
from configs.network import EnvironmentProxy

ConfigReloadFailureReason = Literal[
    "invalid_config",
    "restart_required",
    "apply_failed",
    "conflict",
    "rejected",
    "recovery_required",
]


@dataclass(frozen=True, slots=True)
class ConfigSnapshot:
    revision: str = field(init=False)
    canonical_json: str
    source_paths: tuple[Path, ...]
    loaded_at: datetime
    graph: ConfigGraph
    source_details: tuple[ConfigSource, ...] = ()
    schema_path: Path | None = None
    origins_json: str = "[]"
    read_only: Mapping[str, ConfigValue] = field(init=False, repr=False, compare=False)
    effective_values: Mapping[str, ConfigValue] = field(init=False, repr=False, compare=False)
    network_revision: str = field(init=False)

    def __post_init__(self) -> None:
        """同一不可变快照只解析一次；模型暖读不复制整份工作区配置。"""
        payload = self.to_dict()
        object.__setattr__(self, "read_only", cast(Mapping[str, ConfigValue], freeze(payload)))
        def options(node):
            return {key: thaw(value) for key, value in node.settings.items()
                    if key in {"model_options", "network"}}

        providers = {}
        for provider_id, definition in payload["llm"]["providers"].items():
            providers[provider_id] = {
                **{key: value for key, value in definition.items() if key not in {"config_ref", "models"}},
                "options": options(self.graph.resolve_provider(provider_id)),
                "models": {model_id: {**{key: value for key, value in model.items() if key != "config_ref"},
                    "options": options(self.graph.resolve_model(provider_id, model_id))}
                    for model_id, model in definition["models"].items()},
            }
        effective = {**payload, "llm": {**payload["llm"], "providers": providers}}
        object.__setattr__(self, "effective_values", cast(Mapping[str, ConfigValue], freeze(effective)))
        networks = [payload["network"], *(provider["options"]["network"] for provider in providers.values()),
            *(model["options"]["network"] for provider in providers.values() for model in provider["models"].values())]
        routes = (ProxyRoutes.from_configuration(EnvironmentProxy(), environment=self.graph.proxy_environment)
                  if any(network["proxy"]["mode"] == "environment" for network in networks) else ProxyRoutes(()))
        object.__setattr__(self, "network_revision", routes.revision)
        object.__setattr__(self, "revision", hashlib.sha256(json.dumps(
            effective, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ).encode()).hexdigest())

    def to_dict(self) -> dict[str, Any]:
        value = json.loads(self.canonical_json)
        if not isinstance(value, dict):
            raise TypeError("配置快照根节点必须是对象")
        return value

    def source_origins(self) -> list[dict[str, object]]:
        return json.loads(self.origins_json)


@dataclass(frozen=True, slots=True)
class ConfigReloadStatus:
    healthy: bool
    revision: str
    last_success_at: datetime
    last_attempt_at: datetime
    last_error: str | None
    restart_required: bool = False
    reason: ConfigReloadFailureReason | None = None
    changed_sections: tuple[str, ...] = ()
    state: str | None = None
    active_revision: int | None = None
    pending_revision: int | None = None
    candidate_id: str | None = None
    candidate_ref: str | None = None
    attempt_id: str | None = None
    apply_id: str | None = None
    layer_digests: dict[str, str | None] | None = None
    applied_paths: tuple[str, ...] = ()
    deferred_paths: tuple[str, ...] = ()


class ConfigRestartRequiredError(RuntimeError):
    def __init__(self, message: str, *, changed_sections: tuple[str, ...]) -> None:
        super().__init__(message)
        if not changed_sections:
            raise ValueError("需要重启时必须提供至少一个配置 section")
        self.changed_sections = tuple(dict.fromkeys(changed_sections))


def build_config_snapshot(
    config: dict[str, Any],
    *,
    source_paths: tuple[Path, ...],
    graph: ConfigGraph,
    source_details: tuple[ConfigSource, ...] = (),
    schema_path: Path | None = None,
    origins: list[dict[str, object]] | None = None,
) -> ConfigSnapshot:
    canonical_json = json.dumps(
        config,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return ConfigSnapshot(
        canonical_json=canonical_json,
        source_paths=source_paths,
        loaded_at=datetime.now(UTC),
        graph=graph,
        source_details=source_details
        or tuple(
            ConfigSource(
                uri=None,
                layer="user",
                precedence=index,
                loaded=True,
            )
            for index, _path in enumerate(source_paths)
        ),
        schema_path=schema_path,
        origins_json=json.dumps(sorted(origins or [], key=lambda origin: str(origin["path"])),
                                ensure_ascii=False, sort_keys=True, separators=(",", ":")),
    )
