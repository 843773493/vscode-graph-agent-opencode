"""配置 owner 的提示词文件输入；正文只在候选构建时读取并随快照发布。"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from hashlib import sha256
from pathlib import Path
from typing import cast

from app.core.resource_uri import file_uri_root, parse_resource_uri
from app.services.infrastructure.resource_platform.sources.observed_source import (
    ObservedSourceDescriptor,
    ObservedSourceHandle,
    StableSourceReader,
)
from configs.runtime import validate_config_payload

InstructionFileResolver = Callable[[str], tuple[Path, str]]


def _inputs(
    config: Mapping[str, object],
) -> Iterator[tuple[str, str, dict[str, object], str, str]]:
    agents = config.get("agents", {})
    if not isinstance(agents, Mapping):
        raise TypeError("agents 必须是对象")
    for agent_id, agent in agents.items():
        if not isinstance(agent, Mapping) or not isinstance(
            agent.get("instructions"), dict
        ):
            raise TypeError(f"Agent instructions 必须是对象: agent_id={agent_id}")
        instructions = cast(dict[str, object], agent["instructions"])
        yield str(agent_id), "R01", instructions, "system_prompt", "system_prompt_file"
        identity = instructions.get("runtime_identity")
        if isinstance(identity, dict):
            yield (
                str(agent_id),
                "R02",
                cast(dict[str, object], identity),
                "description",
                "description_file",
            )


def _text(values: Mapping[str, object], key: str) -> str:
    value = values.get(key, "")
    if not isinstance(value, str):
        raise TypeError(f"指令正文必须是字符串: {key}")
    return value


def _file_uri(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError("提示词文件必须使用 files 资源 URI 或 null")
    parsed = parse_resource_uri(value)
    if parsed.kind != "files" or parsed.scope not in {"workspace", "gateway"}:
        raise ValueError("提示词文件必须使用 Workspace 或 Gateway files 资源 URI")
    return parsed.display_uri


class InstructionFileInputs:
    """保留私有读取句柄与可用性；唯一正文仍在 ConfigSnapshot 中。"""

    def __init__(self, resolve_file: InstructionFileResolver | None) -> None:
        self._resolve_file = resolve_file
        self._paths: dict[str, tuple[Path, Path]] = {}
        self._failures: dict[str, Exception] = {}
        self._candidate_uris: tuple[str, ...] = ()

    def _path(self, uri: str, *, refresh: bool = False) -> tuple[Path, Path]:
        if refresh or uri not in self._paths:
            if self._resolve_file is None:
                raise RuntimeError("提示词文件来源没有绑定目标文件 owner")
            path, canonical = self._resolve_file(uri)
            root, _ = self._resolve_file(file_uri_root(uri))
            if canonical != uri:
                raise ValueError("提示词文件 owner 返回了不同资源 URI")
            self._paths[uri] = path, root
        return self._paths[uri]

    def load(
        self, config: Mapping[str, object], *, schema: dict[str, object]
    ) -> tuple[Path, ...]:
        """文件替换选中正文，不拼接 inline；失败让原配置事务明确拒绝。"""
        selected = [
            (agent, field, values, text_key, uri)
            for agent, field, values, text_key, file_key in _inputs(config)
            if (uri := _file_uri(values.get(file_key))) is not None
        ]
        self._candidate_uris = tuple(dict.fromkeys(uri for _, _, _, _, uri in selected))
        contents: dict[str, str] = {}
        for agent, field, values, text_key, uri in selected:
            try:
                path, root = self._path(uri, refresh=True)
                if uri not in contents:
                    identity = f"config-instruction:{agent}:{field}"
                    observed = StableSourceReader().read(
                        ObservedSourceHandle(
                            descriptor=ObservedSourceDescriptor(
                                source_id=identity,
                                source_kind="file",
                                display_uri=uri,
                                entry_identity=identity,
                            ),
                            file_path=str(path),
                            allowed_root=str(root),
                        )
                    )
                    contents[uri] = observed.content
                definitions = cast(dict[str, dict[str, object]], schema["$defs"])
                properties = cast(
                    dict[str, dict[str, object]],
                    definitions["agentInstructions"]["properties"],
                )
                text_schema = (
                    properties["system_prompt"]
                    if field == "R01"
                    else cast(
                        dict[str, dict[str, object]],
                        properties["runtime_identity"]["properties"],
                    )["description"]
                )
                validate_config_payload(
                    {"text": contents[uri]},
                    schema={"properties": {"text": text_schema}},
                )
            except Exception as error:
                # 只记录 readiness 并原样抛出；不伪造空正文或退回 inline。
                self._failures[uri] = error
                raise
            self._failures.pop(uri, None)
            values[text_key] = contents[uri]
        return tuple(self._paths[uri][0] for uri in self._candidate_uris)

    def watch_paths(
        self, config: Mapping[str, object], *, include_candidate: bool = False
    ) -> tuple[Path, ...]:
        uris = [
            _file_uri(values.get(file_key))
            for _, _, values, _, file_key in _inputs(config)
        ]
        if include_candidate:
            # 未进入读取的坏候选没有合法句柄，等待配置修复；不能在 finally
            # 解析它并覆盖本次实际失败的来源错误。
            uris.extend(uri for uri in self._candidate_uris if uri in self._paths)
        return tuple(
            dict.fromkeys(
                self._path(uri)[0]
                for uri in uris
                if uri is not None and (uri in self._paths or uri not in self._failures)
            )
        )

    def require_available(self, config: Mapping[str, object], agent_id: str) -> None:
        for agent, _, values, _, file_key in _inputs(config):
            uri = _file_uri(values.get(file_key))
            if agent == agent_id and uri is not None and uri in self._failures:
                error = self._failures[uri]
                raise RuntimeError(
                    f"instruction-source-unavailable: uri={uri}; {error}"
                ) from error

    def diagnostics(self, config: Mapping[str, object]) -> list[dict[str, object]]:
        return [
            {
                "agent_id": agent,
                "producer_id": field,
                "input": "inline" if uri is None else "file",
                "uri": uri,
                "content_hash": sha256(
                    _text(values, text_key).encode("utf-8")
                ).hexdigest(),
                "available": uri not in self._failures,
                "last_error": type(self._failures[uri]).__name__
                if uri in self._failures
                else None,
            }
            for agent, field, values, text_key, file_key in _inputs(config)
            for uri in (_file_uri(values.get(file_key)),)
        ]
