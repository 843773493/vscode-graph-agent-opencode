"""项目自有服务插件声明、绑定与运行回执合同。"""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from app.schemas.canonical_params import CanonicalWorkspaceId

ExtensionPoint = Literal[
    "commands", "menus", "keybindings", "configuration", "services", "debuggers", "skills",
]
PluginRuntime = Literal["python", "node"]
PluginScope = Literal["gateway", "workspace"]
PluginState = Literal["stopped", "starting", "ready", "stopping", "failed"]


class _PluginValue(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class PluginExtensionRequirement(_PluginValue):
    """插件要求的扩展点合同版本。"""

    extension_point: str = Field(min_length=1, pattern=r"^[a-z][a-z0-9_]*$")
    version: int = Field(gt=0)


class PluginTarget(_PluginValue):
    gateway_id: str = Field(min_length=1)
    gateway_connection_id: str | None = None


class InstallPluginRequest(PluginTarget):
    """从明确目标机器的原生绝对路径导入本地包或显式开发目录。"""

    path: str = Field(min_length=1)
    development_directory: bool = False
    expected_digest: str | None = None


class PluginVersionRequest(PluginTarget):
    package_id: str = Field(min_length=1, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    package_version: str = Field(min_length=1, pattern=r"^[A-Za-z0-9][A-Za-z0-9.+_-]*$")


class RestartPluginRequest(PluginVersionRequest):
    plugin_id: str = Field(min_length=1, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    revision: str = Field(min_length=1)
    workspace_id: CanonicalWorkspaceId | None = None


class PluginServiceContribution(_PluginValue):
    """插件导出的具名业务服务，不包含监听地址或进程信息。"""

    service_id: str = Field(min_length=1, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    service_type: Literal["mcp", "terminal", "browser", "debug"]
    protocol: Literal["streamable-http", "http", "websocket", "dap"]

    @model_validator(mode="after")
    def protocol_matches_service(self) -> PluginServiceContribution:
        allowed = {
            "mcp": {"streamable-http"},
            "terminal": {"http", "websocket"},
            "browser": {"http", "websocket"},
            "debug": {"dap"},
        }
        if self.protocol not in allowed[self.service_type]:
            raise ValueError("服务协议与 service_type 不匹配")
        return self


class PluginToolBinding(_PluginValue):
    tool_id: str = Field(min_length=1, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    contract_version: int = Field(gt=0)
    schema_hash: str | None = Field(default=None, pattern=r"^sha256:[a-f0-9]{64}$")
    service_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class PluginSkillContribution(_PluginValue):
    id: str = Field(min_length=1, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    path: str = Field(min_length=1)
    tool_bindings: list[PluginToolBinding] = Field(default_factory=list)

    @model_validator(mode="after")
    def skill_path_is_relative(self) -> PluginSkillContribution:
        path = PurePosixPath(self.path)
        if ("\\" in self.path or path.is_absolute() or ".." in path.parts or path == PurePosixPath(".")
                or (len(self.path) >= 2 and self.path[0].isalpha() and self.path[1] == ":")):
            raise ValueError("Skill path 必须是包根内的显式相对 POSIX 路径")
        return self


class PluginCommandContribution(_PluginValue):
    id: str = Field(min_length=1, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    title: str = Field(min_length=1)
    category: str | None = None
    handler: str = Field(min_length=1, pattern=r"^[A-Za-z][A-Za-z0-9_.-]*$")
    arguments_schema: dict[str, JsonValue]
    result_schema: dict[str, JsonValue]
    required_capabilities: list[str] = Field(default_factory=list)
    enablement: str | None = None


class PluginMenuContribution(_PluginValue):
    id: str = Field(min_length=1, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    location: str = Field(min_length=1)
    command_id: str = Field(min_length=1, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    group: str | None = None
    order: int | None = None
    when: str | None = None


class PluginKeybindingContribution(_PluginValue):
    id: str = Field(min_length=1, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    command_id: str = Field(min_length=1, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    key: str | None = Field(default=None, min_length=1)
    mac: str | None = Field(default=None, min_length=1)
    linux: str | None = Field(default=None, min_length=1)
    win: str | None = Field(default=None, min_length=1)
    when: str | None = None

    @model_validator(mode="after")
    def has_key_for_a_platform(self) -> PluginKeybindingContribution:
        if not any((self.key, self.mac, self.linux, self.win)):
            raise ValueError("快捷键至少要声明 key 或一个平台键位")
        return self


class PluginDebuggerContribution(_PluginValue):
    type: str = Field(min_length=1, pattern=r"^[A-Za-z][A-Za-z0-9._-]*$")
    label: str = Field(min_length=1)
    configuration_schema: dict[str, JsonValue]
    service_id: str = Field(min_length=1, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    dap_version: int = Field(gt=0)
    capabilities: list[str] = Field(default_factory=list)


class PluginContributions(_PluginValue):
    """首批开放贡献；具体扩展点数据由对应业务 owner 继续校验。"""

    commands: list[PluginCommandContribution] = Field(default_factory=list)
    menus: list[PluginMenuContribution] = Field(default_factory=list)
    keybindings: list[PluginKeybindingContribution] = Field(default_factory=list)
    configuration: dict[str, JsonValue] | None = None
    services: list[PluginServiceContribution] = Field(default_factory=list)
    debuggers: list[PluginDebuggerContribution] = Field(default_factory=list)
    skills: list[PluginSkillContribution] = Field(default_factory=list)

    @model_validator(mode="after")
    def at_least_one_contribution(self) -> PluginContributions:
        if not any((self.commands, self.menus, self.keybindings, self.configuration is not None,
                    self.services, self.debuggers, self.skills)):
            raise ValueError("插件必须声明至少一种已开放贡献")
        return self

    @model_validator(mode="after")
    def contribution_ids_are_unique(self) -> PluginContributions:
        for point, field in (("commands", "id"), ("menus", "id"), ("keybindings", "id"),
                             ("services", "service_id"), ("debuggers", "type"), ("skills", "id")):
            ids = [getattr(item, field) for item in getattr(self, point)]
            if len(ids) != len(set(ids)):
                raise ValueError(f"{point} 中贡献标识不得重复")
        return self


class ServicePluginManifest(_PluginValue):
    id: str = Field(min_length=1, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    name: str = Field(min_length=1)
    instance_scope: PluginScope
    requires: list[PluginExtensionRequirement] = Field(default_factory=list)
    contributes: PluginContributions
    runtime: PluginRuntime | None = None
    entrypoint: list[str] | None = None
    management_protocol_version: Literal[1] | None = None

    @model_validator(mode="after")
    def runtime_fields_match(self) -> ServicePluginManifest:
        from app.gateway.plugins.extension_points import (
            PRODUCT_APPROVED_SKILL_TOOL_IDS,
            validate_extension_requirements,
        )

        validate_extension_requirements(self.requires)
        requirements = {item.extension_point: item.version for item in self.requires}
        if len(requirements) != len(self.requires):
            raise ValueError("requires 中扩展点不得重复")
        contribution_points = {
            name for name in ExtensionPoint.__args__
            if (getattr(self.contributes, name) is not None if name == "configuration"
                else bool(getattr(self.contributes, name)))
        }
        if set(requirements) != contribution_points:
            raise ValueError("requires 必须与实际声明的七类贡献逐项一致")
        command_ids = {item.id for item in self.contributes.commands}
        references = [item.command_id for item in self.contributes.menus]
        references.extend(item.command_id for item in self.contributes.keybindings)
        if unknown := sorted(set(references) - command_ids):
            raise ValueError("菜单和快捷键只能引用本插件命令: " + ", ".join(unknown))
        service_ids = {item.service_id for item in self.contributes.services}
        debugger_services = {item.service_id for item in self.contributes.debuggers}
        if unknown_services := sorted(debugger_services - service_ids):
            raise ValueError("调试器必须引用本插件导出的服务: " + ", ".join(unknown_services))
        for skill in self.contributes.skills:
            for binding in skill.tool_bindings:
                if binding.service_id is None:
                    if binding.tool_id not in PRODUCT_APPROVED_SKILL_TOOL_IDS:
                        raise ValueError(
                            f"Skill 工具没有服务来源，也未列入产品批准集合: {binding.tool_id}"
                        )
                elif binding.service_id not in service_ids:
                    raise ValueError(f"Skill 工具关联引用了本插件未导出的服务: {binding.service_id}")
        if self.runtime is None:
            if self.entrypoint is not None or self.management_protocol_version is not None:
                raise ValueError("纯声明插件不能声明 entrypoint 或管理协议版本")
            if self.contributes.commands or self.contributes.services or self.contributes.debuggers:
                raise ValueError("命令、服务和调试器贡献必须由代码插件提供")
            return self
        if self.entrypoint is None or not self.entrypoint or self.management_protocol_version is None:
            raise ValueError("代码插件必须声明 runtime、entrypoint 和 management_protocol_version")
        entry = self.entrypoint[0]
        path = PurePosixPath(entry)
        if (not entry or "\\" in entry or path.is_absolute() or ".." in path.parts or path == PurePosixPath(".")
                or (len(entry) >= 2 and entry[0].isalpha() and entry[1] == ":")):
            raise ValueError("插件入口必须是包根内的相对 POSIX 路径")
        return self


class PluginPackageManifest(_PluginValue):
    manifest_version: Literal[1] = 1
    package_id: str = Field(min_length=1, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    version: str = Field(min_length=1, pattern=r"^[A-Za-z0-9][A-Za-z0-9.+_-]*$")
    name: str = Field(min_length=1)
    description: str | None = None
    extension_contract_version: Literal[1] = 1
    plugins: list[ServicePluginManifest] = Field(min_length=1)

    @model_validator(mode="after")
    def plugin_ids_are_unique(self) -> PluginPackageManifest:
        plugin_ids = [plugin.id for plugin in self.plugins]
        if len(plugin_ids) != len(set(plugin_ids)):
            raise ValueError("同一插件包内 plugin id 必须唯一")
        return self


class PluginBinding(_PluginValue):
    package_id: str = Field(min_length=1, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    plugin_id: str = Field(min_length=1, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    package_version: str = Field(min_length=1, pattern=r"^[A-Za-z0-9][A-Za-z0-9.+_-]*$")
    workspace_id: CanonicalWorkspaceId | None = None
    enabled: bool


class PluginInstanceView(_PluginValue):
    gateway_id: str = Field(min_length=1)
    package_id: str = Field(min_length=1, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    package_version: str = Field(min_length=1, pattern=r"^[A-Za-z0-9][A-Za-z0-9.+_-]*$")
    plugin_id: str = Field(min_length=1, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    instance_scope: PluginScope
    workspace_id: CanonicalWorkspaceId | None = None
    instance_id: str = Field(min_length=1)
    management_protocol_version: Literal[1]
    extension_contract_version: Literal[1]
    status: PluginState
    process_id: int = Field(gt=0)
    capabilities: list[str] = Field(default_factory=list)
    services: list[PluginServiceContribution] = Field(default_factory=list)

    @model_validator(mode="after")
    def status_matches_scope(self) -> PluginInstanceView:
        if (self.instance_scope == "workspace") != (self.workspace_id is not None):
            raise ValueError("workspace 插件回执必须且只能绑定 workspace_id")
        if len({service.service_id for service in self.services}) != len(self.services):
            raise ValueError("插件回执包含重复 service_id")
        return self


class PluginServiceReference(_PluginValue):
    """一次业务连接冻结的服务身份，不含目标监听地址。"""

    gateway_id: str = Field(min_length=1)
    package_id: str = Field(min_length=1)
    package_version: str = Field(min_length=1)
    plugin_id: str = Field(min_length=1)
    instance_id: str = Field(min_length=1)
    service_id: str = Field(min_length=1)
    workspace_id: CanonicalWorkspaceId | None = None


class PluginInstanceStatus(PluginInstanceView):
    log_sink: dict[str, JsonValue] | None = None


class InstalledPluginPackageDTO(_PluginValue):
    manifest: PluginPackageManifest
    digest: str


class PluginSourceStatusDTO(_PluginValue):
    configured: bool
    message: str


class PluginInventoryResultDTO(_PluginValue):
    packages: list[InstalledPluginPackageDTO]
    sources: PluginSourceStatusDTO


class PluginSlotStatusDTO(_PluginValue):
    gateway_id: str = Field(min_length=1)
    binding: PluginBinding
    instance_scope: PluginScope
    revision: str
    state: PluginState
    error: str | None = None
    declarative: bool
    instance: PluginInstanceView | None = None


class WorkspacePluginConfigurationDTO(_PluginValue):
    workspace_id: CanonicalWorkspaceId
    state: Literal["waiting", "ready", "stopped", "failed"]
    revision: str | None = None
    error: str | None = None


class PluginRuntimeStatusDTO(_PluginValue):
    plugins: list[PluginSlotStatusDTO]
    configured_bindings: list[PluginBinding]
    workspace_configurations: list[WorkspacePluginConfigurationDTO] = Field(default_factory=list)


class PluginUninstallResultDTO(_PluginValue):
    package_id: str
    package_version: str
    uninstalled: bool


__all__ = [
    "ExtensionPoint",
    "InstallPluginRequest",
    "InstalledPluginPackageDTO",
    "PluginBinding",
    "PluginCommandContribution",
    "PluginContributions",
    "PluginDebuggerContribution",
    "PluginExtensionRequirement",
    "PluginInstanceStatus",
    "PluginInstanceView",
    "PluginInventoryResultDTO",
    "PluginKeybindingContribution",
    "PluginMenuContribution",
    "PluginPackageManifest",
    "PluginRuntime",
    "PluginRuntimeStatusDTO",
    "PluginScope",
    "PluginServiceContribution",
    "PluginServiceReference",
    "PluginSkillContribution",
    "PluginSlotStatusDTO",
    "PluginSourceStatusDTO",
    "PluginState",
    "PluginTarget",
    "PluginToolBinding",
    "PluginUninstallResultDTO",
    "PluginVersionRequest",
    "RestartPluginRequest",
    "ServicePluginManifest",
    "WorkspacePluginConfigurationDTO",
]
