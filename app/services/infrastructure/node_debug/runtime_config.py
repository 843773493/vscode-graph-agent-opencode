from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class NodeDebugNodeRuntimeConfig:
    inspector_host: str
    inspector_port: int
    executable: str


@dataclass(frozen=True, slots=True)
class NodeDebugLaunchProfileConfig:
    adapter: str
    runtime: str
    program: str
    working_directory: str
    args: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class NodeDebugRuntimeConfig:
    enabled: bool
    default_adapter: str
    command_timeout_seconds: float
    node: NodeDebugNodeRuntimeConfig
    launch_profiles: dict[str, NodeDebugLaunchProfileConfig]

    @classmethod
    def from_mapping(cls, value: object) -> NodeDebugRuntimeConfig:
        if not isinstance(value, Mapping):
            raise TypeError("runtime.debug 配置无效")
        enabled = value.get("enabled")
        default_adapter = value.get("default_adapter")
        timeout = value.get("command_timeout_seconds")
        if not isinstance(enabled, bool):
            raise TypeError("runtime.debug.enabled 配置无效")
        if not isinstance(default_adapter, str):
            raise TypeError("runtime.debug.default_adapter 配置无效")
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
            raise TypeError("runtime.debug.command_timeout_seconds 配置无效")
        node = _node_config(value.get("node"))
        raw_profiles = value.get("launch_profiles")
        if not isinstance(raw_profiles, Mapping):
            raise TypeError("runtime.debug.launch_profiles 配置无效")
        profiles: dict[str, NodeDebugLaunchProfileConfig] = {}
        for name, raw_profile in raw_profiles.items():
            if not isinstance(name, str) or not name:
                raise TypeError("runtime.debug.launch_profiles 名称无效")
            if not isinstance(raw_profile, Mapping):
                raise TypeError(f"runtime.debug.launch_profiles.{name} 配置无效")
            profiles[name] = _profile_config(name, raw_profile)
        return cls(
            enabled=enabled,
            default_adapter=default_adapter,
            command_timeout_seconds=float(timeout),
            node=node,
            launch_profiles=profiles,
        )

    def resolve_profile(
        self,
        requested_name: str | None,
    ) -> tuple[str, NodeDebugLaunchProfileConfig]:
        profile_name = requested_name or "node-default"
        profile = self.launch_profiles.get(profile_name)
        if profile is None:
            raise TypeError(f"调试启动配置不存在: {profile_name}")
        return profile_name, profile


def _node_config(value: object) -> NodeDebugNodeRuntimeConfig:
    if not isinstance(value, Mapping):
        raise TypeError("runtime.debug.node 配置无效")
    host = value.get("inspector_host")
    port = value.get("inspector_port")
    executable = value.get("executable")
    if not isinstance(host, str) or not isinstance(executable, str):
        raise TypeError("runtime.debug.node 配置字段无效")
    if isinstance(port, bool) or not isinstance(port, int):
        raise TypeError("runtime.debug.node.inspector_port 配置无效")
    return NodeDebugNodeRuntimeConfig(
        inspector_host=host,
        inspector_port=port,
        executable=executable,
    )


def _profile_config(
    name: str,
    value: Mapping[object, object],
) -> NodeDebugLaunchProfileConfig:
    adapter = value.get("adapter")
    runtime = value.get("runtime")
    program = value.get("program")
    working_directory = value.get("working_directory")
    args = value.get("args")
    if (
        not isinstance(adapter, str)
        or not isinstance(runtime, str)
        or not isinstance(program, str)
    ):
        raise TypeError(f"runtime.debug.launch_profiles.{name} 字段无效")
    if not isinstance(working_directory, str):
        raise TypeError(f"runtime.debug.launch_profiles.{name}.working_directory 无效")
    if not isinstance(args, list) or not all(isinstance(argument, str) for argument in args):
        raise TypeError(f"runtime.debug.launch_profiles.{name}.args 无效")
    return NodeDebugLaunchProfileConfig(
        adapter=adapter,
        runtime=runtime,
        program=program,
        working_directory=working_directory,
        args=list(args),
    )


__all__ = [
    "NodeDebugLaunchProfileConfig",
    "NodeDebugNodeRuntimeConfig",
    "NodeDebugRuntimeConfig",
]
