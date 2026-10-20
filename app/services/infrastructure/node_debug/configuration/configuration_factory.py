from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

from app.core.exceptions import ForbiddenError
from app.core.identifier import create_prefixed_id
from app.schemas.internal_v2.node_debug import (
    NodeDebugBreakpointDTO,
    NodeDebugBreakpointRequest,
    NodeDebugConfigurationDTO,
)
from app.services.infrastructure.node_debug.breakpoint.breakpoint_expressions import (
    inspector_breakpoint_condition,
)
from app.services.infrastructure.node_debug.breakpoint.breakpoints import (
    anchor_breakpoint,
    persistable_breakpoint,
    portable_breakpoint,
    reconcile_breakpoint,
    runtime_breakpoint,
)
from app.services.infrastructure.workspace_files.paths import WorkspaceFilePaths

_SUPPORTED_EXTENSIONS = {".cjs", ".js", ".mjs"}


class NodeDebugConfigurationFactory:
    """构造并规范化 Node Debug 配置及其源码断点。

    该对象只负责工作区边界内的路径、参数和配置 DTO 规范化，不持有运行时
    或调试进程状态。服务和配置注册表共享同一个 factory，确保持久化读取、
    API 请求和启动运行时使用同一套字段规则。
    """

    def __init__(self, *, file_paths: WorkspaceFilePaths) -> None:
        self._workspace_root = file_paths.workspace.resolve()
        self._file_paths = file_paths

    def resolve_workspace_path(
        self, raw_path: str, *, script: bool = False,
    ) -> tuple[Path, str]:
        """输入在目标文件 owner 解析；能力投影允许引用尚未创建的文件。"""
        normalized = raw_path.strip()
        if script and "://" not in normalized:
            normalized = normalized.replace("\\", "/")
        target, uri = self._file_paths.resolve(normalized, allow_external=False)
        if not target.is_relative_to(self._workspace_root):
            raise ForbiddenError("调试路径必须位于当前 workspace 内")
        return target, uri

    def workspace_relative_path(self, raw_path: str, field_name: str = "path") -> str:
        """模型参数只做输入约束；目标解析仍由同一文件 owner 执行。"""
        if not raw_path:
            raise ValueError(f"{field_name} 不能为空")
        if "://" not in raw_path and ".." in Path(raw_path).parts:
            raise ValueError(f"{field_name} 不是有效的工作区相对路径: 不允许上级目录")
        try:
            target, _ = self.resolve_workspace_path(raw_path)
        except ForbiddenError as error:
            raise ValueError(f"{field_name} 超出当前工作区工具的访问能力") from error
        relative = target.relative_to(self._workspace_root).as_posix()
        if os.name != "nt" and "\\" in relative:
            raise ValueError(f"{field_name} 含目标平台的字面反斜杠，源码调试不支持该文件名")
        return relative

    def validate_configuration(
        self,
        configuration: NodeDebugConfigurationDTO,
    ) -> NodeDebugConfigurationDTO:
        if configuration.script_path is not None:
            _, relative_path = self.resolve_script_path(configuration.script_path)
            configuration = configuration.model_copy(
                update={"script_path": relative_path}
            )
        resolved_directory = self.resolve_working_directory(
            configuration.working_directory
        )
        relative_directory = (
            resolved_directory.relative_to(self._workspace_root).as_posix()
            if resolved_directory != self._workspace_root
            else ""
        )
        normalized_breakpoints: list[NodeDebugBreakpointDTO] = []
        for breakpoint in configuration.breakpoints:
            breakpoint_path, relative_path = self.resolve_script_path(breakpoint.path)
            normalized_breakpoints.append(
                persistable_breakpoint(
                    reconcile_breakpoint(
                        runtime_breakpoint(
                            breakpoint.model_copy(update={"path": relative_path})
                        ),
                        breakpoint_path,
                    )
                )
            )
        return configuration.model_copy(
            update={
                "name": configuration.name.strip(),
                "working_directory": relative_directory,
                "args": self.normalize_args(configuration.args),
                "breakpoints": [
                    portable_breakpoint(breakpoint)
                    for breakpoint in normalized_breakpoints
                ],
            }
        )

    def configuration_from_request(
        self,
        *,
        configuration_id: str,
        name: str,
        script_path: str | None,
        working_directory: str,
        launch_profile_name: str | None,
        args: list[str],
        breakpoints: list[NodeDebugBreakpointRequest],
        revision: int = 1,
        created_at: datetime | None = None,
    ) -> NodeDebugConfigurationDTO:
        now = datetime.now(UTC)
        configuration = NodeDebugConfigurationDTO(
            configuration_id=configuration_id,
            name=name.strip(),
            revision=revision,
            script_path=script_path,
            working_directory=working_directory,
            launch_profile_name=launch_profile_name,
            args=list(args),
            breakpoints=[
                portable_breakpoint(
                    self.create_breakpoint(
                        path=breakpoint.path,
                        line=breakpoint.line,
                        column=breakpoint.column,
                        condition=breakpoint.condition,
                        hit_condition=breakpoint.hit_condition,
                        log_message=breakpoint.log_message,
                    )
                )
                for breakpoint in breakpoints
            ],
            created_at=created_at or now,
            updated_at=now,
        )
        return self.validate_configuration(configuration)

    def create_breakpoint(
        self,
        *,
        path: str,
        line: int,
        column: int,
        condition: str | None,
        hit_condition: int | None = None,
        log_message: str | None = None,
    ) -> NodeDebugBreakpointDTO:
        script_path, relative_path = self.resolve_script_path(path)
        breakpoint = NodeDebugBreakpointDTO(
            breakpoint_id=create_prefixed_id("node-bp"),
            path=relative_path,
            line=line,
            column=column,
            condition=condition.strip() or None if condition is not None else None,
            hit_condition=hit_condition,
            log_message=log_message,
            original_line=line,
            created_at=datetime.now(UTC),
        )
        inspector_breakpoint_condition(
            breakpoint_id=breakpoint.breakpoint_id,
            condition=breakpoint.condition,
            hit_condition=breakpoint.hit_condition,
            log_message=breakpoint.log_message,
        )
        return anchor_breakpoint(breakpoint, script_path)

    def resolve_script_path(self, raw_path: str) -> tuple[Path, str]:
        if not raw_path.strip():
            raise ValueError("Node 调试脚本路径不能为空")
        script_path, _ = self.resolve_workspace_path(raw_path, script=True)
        if script_path.suffix.lower() not in _SUPPORTED_EXTENSIONS:
            raise ValueError("Node 调试目前只支持 .js、.mjs 和 .cjs 文件")
        if not script_path.is_file():
            raise FileNotFoundError(f"Node 调试脚本不存在: {raw_path}")
        relative_path = script_path.relative_to(self._workspace_root).as_posix()
        return script_path, relative_path

    def resolve_working_directory(self, raw_path: str) -> Path:
        try:
            resolved, _ = self.resolve_workspace_path(raw_path)
        except ForbiddenError as error:
            if Path(raw_path.strip()).is_absolute():
                raise ValueError(
                    f"调试工作目录必须位于当前 workspace 内: {raw_path}"
                ) from error
            raise
        if not resolved.is_dir():
            raise FileNotFoundError(f"调试工作目录不存在: {raw_path}")
        return resolved

    @staticmethod
    def normalize_args(args: list[str]) -> list[str]:
        if len(args) > 20:
            raise ValueError("Node 调试参数最多 20 个")
        for argument in args:
            if not isinstance(argument, str):
                raise TypeError("Node 调试参数必须全部是字符串")
        return args


__all__ = ["NodeDebugConfigurationFactory"]
