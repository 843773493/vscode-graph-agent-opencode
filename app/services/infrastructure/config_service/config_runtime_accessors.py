"""ConfigRuntimeAccessorsMixin：ConfigService 的事件游标、agent-runtime 与标量访问器方法族（纯搬迁）。"""

from __future__ import annotations

from typing import Any


class ConfigRuntimeAccessorsMixin:
    def list_config_events(
        self,
        *,
        after: int = 0,
        limit: int = 100,
    ):
        return self._event_cursor.list_config_events(after=after, limit=limit)

    def claim_config_events_for_consumer(
        self,
        *,
        after: int,
        consumer_id: str,
        limit: int = 100,
    ):
        return self._event_cursor.claim_config_events_for_consumer(
            after=after,
            consumer_id=consumer_id,
            limit=limit,
        )

    def mark_config_event_delivered_for_consumer(
        self,
        *,
        event_id: str,
        consumer_id: str,
    ):
        return self._event_cursor.mark_config_event_delivered_for_consumer(
            event_id=event_id,
            consumer_id=consumer_id,
        )

    def ensure_config_event_cursor(self, *, after: int) -> None:
        self._event_cursor.ensure_config_event_cursor(after=after)

    def get_agent_run_timeout_seconds(self) -> float:
        """返回单个 Agent Job 的总执行上限。"""
        value = self._read_runtime_value(("agent", "run", "timeout_seconds"))
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(
                "runtime.agent.run.timeout_seconds 必须是正数"
            )
        if value <= 0:
            raise ValueError(
                "runtime.agent.run.timeout_seconds 必须大于 0"
            )
        return float(value)

    def get_agent_run_mode(self) -> str:
        """返回 Agent 运行模式，决定是否向模型暴露团队面板工具。"""
        value = self._read_runtime_value(("agent", "run", "mode"))
        if value not in {"single_agent", "team"}:
            raise ValueError(
                "runtime.agent.run.mode 必须是 single_agent 或 team"
            )
        return value

    def _read_runtime_value(self, path: tuple[str, ...]) -> object:
        current: object = self._get_effective_config().get("runtime", {})
        for key in path:
            if not isinstance(current, dict):
                raise ValueError(
                    f"运行时配置路径不是对象: runtime.{'.'.join(path)}"
                )
            if key not in current:
                raise ValueError(
                    f"内置运行时配置缺少参数: runtime.{'.'.join(path)}"
                )
            current = current[key]
        return current

    def get_debug_runtime_config(self) -> dict[str, Any]:
        """返回经过边界校验的源码调试运行时配置。"""
        runtime = self._get_effective_config().get("runtime", {})
        if runtime is None:
            runtime = {}
        if not isinstance(runtime, dict):
            raise ValueError("runtime 配置必须是对象")

        raw_debug = runtime.get("debug", {})
        if raw_debug is None:
            raw_debug = {}
        if not isinstance(raw_debug, dict):
            raise ValueError("runtime.debug 配置必须是对象")

        enabled = raw_debug.get("enabled", True)
        if not isinstance(enabled, bool):
            raise ValueError("runtime.debug.enabled 必须是布尔值")

        allowed_adapters = {"node_inspector", "debugpy", "dap", "vscode"}
        default_adapter = raw_debug.get("default_adapter", "node_inspector")
        if default_adapter not in allowed_adapters:
            raise ValueError(
                "runtime.debug.default_adapter 不受支持: "
                f"{default_adapter!r}"
            )

        command_timeout_seconds = raw_debug.get("command_timeout_seconds", 10)
        if (
            isinstance(command_timeout_seconds, bool)
            or not isinstance(command_timeout_seconds, (int, float))
            or command_timeout_seconds <= 0
        ):
            raise ValueError(
                "runtime.debug.command_timeout_seconds 必须是正数"
            )

        node = self._parse_debug_endpoint_config(
            raw_debug.get("node"),
            namespace="runtime.debug.node",
            host_key="inspector_host",
            port_key="inspector_port",
            default_host="127.0.0.1",
        )
        executable = node.get("executable", "")
        if not isinstance(executable, str):
            raise ValueError("runtime.debug.node.executable 必须是字符串")
        node["executable"] = executable

        raw_python = raw_debug.get("python")
        if raw_python is None:
            raw_python = {}
        if not isinstance(raw_python, dict):
            raise ValueError("runtime.debug.python 配置必须是对象")
        python_adapter = raw_python.get("adapter", "debugpy")
        if python_adapter != "debugpy":
            raise ValueError(
                "runtime.debug.python.adapter 目前只支持 debugpy"
            )
        python = self._parse_debug_endpoint_config(
            raw_python,
            namespace="runtime.debug.python",
            host_key="debugpy_host",
            port_key="debugpy_port",
            default_host="127.0.0.1",
        )
        python["adapter"] = "debugpy"

        raw_profiles = raw_debug.get("launch_profiles", {})
        if raw_profiles is None:
            raw_profiles = {}
        if not isinstance(raw_profiles, dict):
            raise ValueError("runtime.debug.launch_profiles 配置必须是对象")
        profiles: dict[str, dict[str, Any]] = {}
        for profile_name, raw_profile in raw_profiles.items():
            if not isinstance(profile_name, str) or not profile_name:
                raise ValueError("runtime.debug.launch_profiles 的名称必须是非空字符串")
            if not isinstance(raw_profile, dict):
                raise ValueError(
                    f"runtime.debug.launch_profiles.{profile_name} 必须是对象"
                )
            profile_adapter = raw_profile.get("adapter", default_adapter)
            if profile_adapter not in allowed_adapters:
                raise ValueError(
                    f"runtime.debug.launch_profiles.{profile_name}.adapter 不受支持: "
                    f"{profile_adapter!r}"
                )
            profile_runtime = raw_profile.get(
                "runtime",
                "node" if profile_adapter == "node_inspector" else "",
            )
            if not isinstance(profile_runtime, str) or not profile_runtime.strip():
                raise ValueError(
                    "runtime.debug.launch_profiles."
                    f"{profile_name}.runtime 必须是非空字符串"
                )
            profile_program = raw_profile.get("program", "")
            if not isinstance(profile_program, str):
                raise ValueError(
                    f"runtime.debug.launch_profiles.{profile_name}.program 必须是字符串"
                )
            working_directory = raw_profile.get("working_directory", "")
            if not isinstance(working_directory, str):
                raise ValueError(
                    "runtime.debug.launch_profiles."
                    f"{profile_name}.working_directory 必须是字符串"
                )
            args = raw_profile.get("args", [])
            if not isinstance(args, list) or len(args) > 20 or not all(
                isinstance(argument, str) for argument in args
            ):
                raise ValueError(
                    "runtime.debug.launch_profiles."
                    f"{profile_name}.args 必须是最多 20 个字符串的数组"
                )
            profiles[profile_name] = {
                "adapter": profile_adapter,
                "runtime": profile_runtime,
                "program": profile_program,
                "working_directory": working_directory,
                "args": list(args),
            }

        if not profiles:
            default_runtime = {
                "node_inspector": "node",
                "debugpy": "python",
                "dap": "",
                "vscode": "vscode",
            }[default_adapter]
            profiles["node-default"] = {
                "adapter": default_adapter,
                "runtime": default_runtime,
                "program": "",
                "working_directory": "",
                "args": [],
            }

        return {
            "enabled": enabled,
            "default_adapter": default_adapter,
            "command_timeout_seconds": float(command_timeout_seconds),
            "node": node,
            "python": python,
            "launch_profiles": profiles,
        }

    @staticmethod
    def _parse_debug_endpoint_config(
        raw_value: object,
        *,
        namespace: str,
        host_key: str,
        port_key: str,
        default_host: str,
    ) -> dict[str, Any]:
        if raw_value is None:
            raw_value = {}
        if not isinstance(raw_value, dict):
            raise ValueError(f"{namespace} 配置必须是对象")
        host = raw_value.get(host_key, default_host)
        if not isinstance(host, str) or not host.strip():
            raise ValueError(f"{namespace}.{host_key} 必须是非空字符串")
        if host not in {"127.0.0.1", "localhost", "::1", "[::1]"}:
            raise ValueError(
                f"{namespace}.{host_key} 必须是 loopback 地址，实际值: {host!r}"
            )
        port = raw_value.get(port_key, 0)
        if (
            isinstance(port, bool)
            or not isinstance(port, int)
            or not 0 <= port <= 65535
        ):
            raise ValueError(
                f"{namespace}.{port_key} 必须是 0-65535 的整数"
            )
        return {host_key: host, port_key: port}

    def get_gateway_connection_url(self) -> str:
        value = self._read_runtime_value(("gateway", "connection", "url"))
        if not isinstance(value, str) or not value.strip():
            raise ValueError("runtime.gateway.connection.url 必须是非空字符串")
        return value

    def get_gateway_connection_timeout_seconds(self) -> float:
        value = self._read_runtime_value(
            ("gateway", "connection", "timeout_seconds")
        )
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
            raise ValueError(
                "runtime.gateway.connection.timeout_seconds 必须是正数"
            )
        return float(value)

    def get_terminal_backend_url(self) -> str:
        value = self._read_runtime_value(
            ("auxiliary_services", "terminal_backend", "url")
        )
        if not isinstance(value, str) or not value.strip():
            raise ValueError(
                "runtime.auxiliary_services.terminal_backend.url 必须是非空字符串"
            )
        return value

    def get_browser_backend_url(self) -> str:
        value = self._read_runtime_value(
            ("auxiliary_services", "browser_backend", "url")
        )
        if not isinstance(value, str) or not value.strip():
            raise ValueError(
                "runtime.auxiliary_services.browser_backend.url 必须是非空字符串"
            )
        return value

    def get_workspace_file_default_limit(self) -> int:
        value = self._read_runtime_value(
            ("workspace", "files", "list", "default_limit")
        )
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or value < 1
            or value > 1000
        ):
            raise ValueError(
                "runtime.workspace.files.list.default_limit 必须是 1-1000 的整数"
            )
        return value

    def get_workspace_preview_max_bytes(self) -> int:
        value = self._read_runtime_value(("workspace", "files", "preview", "max_bytes"))
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError(
                "runtime.workspace.files.preview.max_bytes 必须是正整数"
            )
        return value

    def get_workspace_preview_binary_sample_bytes(self) -> int:
        value = self._read_runtime_value(
            ("workspace", "files", "preview", "binary_sample_bytes")
        )
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError(
                "runtime.workspace.files.preview.binary_sample_bytes 必须是正整数"
            )
        return value

    def get_trace_stream_heartbeat_interval_seconds(self) -> float:
        value = self._read_runtime_value(
            ("events", "trace_stream", "heartbeat_interval_seconds")
        )
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
            raise ValueError(
                "runtime.events.trace_stream.heartbeat_interval_seconds 必须是正数"
            )
        return float(value)


__all__ = ["ConfigRuntimeAccessorsMixin"]
