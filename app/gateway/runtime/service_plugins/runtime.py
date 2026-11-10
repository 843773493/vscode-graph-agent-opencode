"""自有插件绑定的进程句柄；复用 Gateway 原进程组、日志及接管。"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import cast
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
import psutil
from jsonschema import Draft202012Validator
from pydantic import BaseModel, ConfigDict, Field, JsonValue

from app.core.config_sources import config_revision
from app.core.env import get_project_root
from app.core.file_permissions import restrict_private_path_access
from app.core.shielded_cleanup import run_cleanup_shielded, run_sync_shielded
from app.gateway.plugins.dependencies import validate_runtime_target
from app.gateway.plugins.packages import PluginPackageStore
from app.gateway.plugins.sources import plugin_sources
from app.gateway.runtime.process_lifecycle import (
    AdoptedManagedProcess,
    ManagedProcess,
    _spawn_logged_process,
    wait_process_exit,
)
from app.gateway.runtime.process_logs import ProcessLogStore, runtime_log_id
from app.gateway.runtime.process_ports import allocate_local_port
from app.gateway.runtime.skill_sources import GatewaySkillSourceOwner
from app.gateway.runtime.workspace_process import (
    _local_child_environment,
    resolve_python_executable,
)
from app.runtime.windows_fs import native_process_path
from app.schemas.gateway.diagnostics import PluginRuntimeObservationDTO
from app.schemas.gateway.plugin_commands import (
    PluginCommandPublication,
    PluginCommandRegistration,
    PluginCommandRegistrationsDTO,
    PluginCommandSnapshotDTO,
)
from app.schemas.gateway.service_plugin import (
    PluginBinding,
    PluginInstanceStatus,
    PluginInstanceView,
    ServicePluginManifest,
)
from app.schemas.runtime_observation import RuntimeObservationStatus
from app.services.infrastructure.config.plugin_private_inputs import (
    PluginPrivateInputStore,
    requires_private_delivery,
    validate_plugin_parameters,
)
from configs.plugin_parameters import (
    PluginParameters,
    plugin_parameters,
)

logger = logging.getLogger(__name__)
Slot = tuple[str, str, str | None]


class _ProcessRecord(BaseModel):
    """原句柄的接管材料，只用于找回实例，不能作为 enabled 来源。"""

    model_config = ConfigDict(extra="forbid", strict=True)
    binding: PluginBinding
    revision: str
    parameters_revision: str
    process_id: int = Field(gt=0)
    created_at: float
    instance_id: str
    base_url: str
    token: str


def gateway_plugin_bindings(payload: dict[str, object]) -> list[PluginBinding]:
    plugins = payload.get("plugins", {})
    if not isinstance(plugins, dict):
        raise TypeError("plugins 必须是对象")
    runtime = plugins.get("runtime", {})
    if not isinstance(runtime, dict):
        raise TypeError("plugins.runtime 必须是对象")
    bindings = [PluginBinding.model_validate(entry) for entry in runtime.get("entries", [])]
    keys = [(binding.package_id, binding.plugin_id) for binding in bindings]
    if len(keys) != len(set(keys)) or any(binding.workspace_id is not None for binding in bindings):
        raise ValueError("Gateway 插件绑定必须唯一且不能包含 workspace_id")
    return bindings


def workspace_default_bindings(payload: dict[str, object]) -> list[PluginBinding]:
    """模板沿原配置数组替换，不充当当前工作区绑定。"""
    plugins = payload.get("plugins", {})
    if not isinstance(plugins, dict):
        raise TypeError("plugins 必须是对象")
    defaults = plugins.get("workspace_defaults", {})
    if not isinstance(defaults, dict):
        raise TypeError("plugins.workspace_defaults 必须是对象")
    return gateway_plugin_bindings({"plugins": {"runtime": defaults}})


@dataclass(slots=True)
class PluginSlot:
    binding: PluginBinding
    revision: str
    declaration: ServicePluginManifest
    gateway_id: str
    package_digest: str = field(kw_only=True)
    parameters: dict[str, JsonValue] = field(default_factory=dict)
    parameters_revision: str = field(init=False)
    process: ManagedProcess | AdoptedManagedProcess | None = None
    process_version: str | None = None
    receipt: PluginInstanceStatus | None = None
    log_id: str | None = None
    base_url: str | None = None
    token: str | None = None
    error: str | None = None
    state: str = "stopped"
    stopped: asyncio.Event = field(default_factory=asyncio.Event)
    commands: dict[str, PluginCommandRegistration] = field(default_factory=dict)
    observation_error: str | None = None
    exit_waiter: asyncio.Task[None] | None = None
    command_validators: dict[str, tuple[Draft202012Validator, Draft202012Validator]] = field(init=False)

    def __post_init__(self) -> None:
        self.parameters_revision = config_revision({"parameters": self.parameters})
        self.command_validators = {command.id: (Draft202012Validator(command.arguments_schema),
                                              Draft202012Validator(command.result_schema))
                                   for command in self.declaration.contributes.commands}

    def describe(self) -> dict[str, object]:
        if self.process is not None and self.state == "ready" and not self.process.is_alive():
            self.state, self.error = "failed", "插件进程已退出；需显式重启，不自动重放在途操作"
        return {"gateway_id": self.gateway_id, "instance_scope": self.declaration.instance_scope,
                "binding": self.binding.model_dump(mode="json"), "revision": self.revision,
                "state": self.state, "error": self.error,
                "declarative": self.declaration.runtime is None,
                "log_id": self.log_id,
                "instance": self.receipt.model_dump(mode="json", exclude={"log_sink"}) if self.receipt else None}


class GatewayPluginRuntime:
    """配置只在原 owner 持有；这里仅保有当前绑定投影和实际进程句柄。"""

    def __init__(self, *, home: Path, gateway_id: str, packages: PluginPackageStore,
                 settings: Callable[[], dict[str, object]], workspace_root: Callable[[str], Path],
                 skill_sources: GatewaySkillSourceOwner) -> None:
        self.gateway_id, self.packages, self.settings = gateway_id, packages, settings
        self.workspace_root = workspace_root
        self.skill_sources = skill_sources
        self.directory = home / "state/gateway/service-plugins"
        self.logs = ProcessLogStore(home / "state/logs")
        self.slots: dict[Slot, PluginSlot] = {}
        self._locks: dict[Slot, asyncio.Lock] = {}
        self.client = httpx.AsyncClient(trust_env=False, timeout=10, follow_redirects=False)
        self.private_inputs = PluginPrivateInputStore(directory=home / "credentials/plugins",
            gateway_id=gateway_id, workspace_id=None)
        self.workspace_parameters: Callable[[PluginBinding, str], Awaitable[dict[str, JsonValue]]] | None = None
        self.changed = asyncio.Event()

    def _publish(self) -> None:
        changed, self.changed = self.changed, asyncio.Event()
        changed.set()

    def command_snapshot(self, workspace_id: str | None) -> PluginCommandSnapshotDTO:
        publications = []
        for slot in self.slots.values():
            if not slot.binding.enabled or slot.binding.workspace_id not in {None, workspace_id}:
                continue
            contributes = slot.declaration.contributes
            if not (contributes.commands or contributes.menus or contributes.keybindings):
                continue
            actual = slot.describe()
            ready = actual["state"] == "ready" and not slot.stopped.is_set()
            publications.append(PluginCommandPublication(binding=slot.binding, revision=slot.revision,
                state=str(actual["state"]), error=slot.error,
                instance_id=slot.receipt.instance_id if ready and slot.receipt is not None else None,
                commands=contributes.commands, registrations=list(slot.commands.values()) if ready else [],
                menus=contributes.menus, keybindings=contributes.keybindings))
        return PluginCommandSnapshotDTO(gateway_id=self.gateway_id, workspace_id=workspace_id,
                                       publications=publications)

    def _watch_exit(self, slot: PluginSlot) -> None:
        assert slot.receipt is not None
        process_id = slot.receipt.process_id

        async def watch() -> None:
            try:
                await wait_process_exit(process_id)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                slot.error = f"插件退出通知失败: {error}"
                logger.exception("插件退出等待失败: binding=%s", slot.binding)
            else:
                if isinstance(slot.process, ManagedProcess):
                    # 系统退出通知不收割 Popen 子进程；在发布 failed 前回收已退出的根。
                    slot.process.process.poll()
                slot.error = "插件进程已退出；需显式重启，不自动重放在途操作"
            slot.state = "failed"
            slot.stopped.set()
            self._publish()

        slot.exit_waiter = asyncio.create_task(watch())

    async def _release_exit_waiter(self, slot: PluginSlot) -> None:
        if slot.exit_waiter is not None:
            slot.exit_waiter.cancel()
            await asyncio.gather(slot.exit_waiter, return_exceptions=True)
            slot.exit_waiter = None

    @staticmethod
    def key(binding: PluginBinding) -> Slot:
        return binding.package_id, binding.plugin_id, binding.workspace_id

    def declaration(self, binding: PluginBinding) -> ServicePluginManifest:
        package = self.packages.get(binding.package_id, binding.package_version)
        matches = [plugin for plugin in package.manifest.plugins if plugin.id == binding.plugin_id]
        if len(matches) != 1:
            raise ValueError(f"包不包含插件: {binding.package_id}/{binding.plugin_id}")
        plugin = matches[0]
        if (plugin.instance_scope == "workspace") != (binding.workspace_id is not None):
            raise ValueError("插件绑定与 manifest 固定范围不符")
        return plugin

    def validate_target(self, binding: PluginBinding) -> None:
        package = self.packages.get(binding.package_id, binding.package_version)
        # 安装校验整包；运行准入只核当前插件，不能让兄弟插件的环境要求串扰。
        runtimes = {entry.runtime for entry in package.manifest.plugins
                    if entry.id == binding.plugin_id and entry.runtime is not None}
        if not runtimes:
            return
        node_executable = os.environ.get("BOXTEAM_NODE_BIN")
        if "node" in runtimes and (not node_executable or not Path(node_executable).is_absolute()):
            raise ValueError("Node 插件需要已配置的绝对 BOXTEAM_NODE_BIN，不临时发现或下载")
        if package.manifest.target is None:
            raise RuntimeError("代码插件包缺少目标运行环境")
        validate_runtime_target(package.manifest.target, runtimes=runtimes,
            node_executable=node_executable, python_executable=resolve_python_executable())
        self.packages.validate_runtime_dependencies(binding.package_id, binding.package_version,
            plugin_id=binding.plugin_id)

    def validate(self, payload: dict[str, object]) -> None:
        plugin_sources(payload)
        self.validate_bindings(gateway_plugin_bindings(payload), workspace_id=None,
                               parameters=plugin_parameters(payload))
        self.validate_defaults(payload)

    def validate_defaults(self, payload: dict[str, object]) -> None:
        for binding in workspace_default_bindings(payload):
            package = self.packages.get(binding.package_id, binding.package_version)
            plugin = next((entry for entry in package.manifest.plugins if entry.id == binding.plugin_id), None)
            if plugin is None or plugin.instance_scope != "workspace":
                raise ValueError(f"新建工作区默认值只接受已安装的 workspace 插件: {binding.package_id}/{binding.plugin_id}")

    def validate_bindings(self, bindings: list[PluginBinding], *, workspace_id: str | None,
                          parameters: PluginParameters | None = None,
                          delivered: PluginParameters | None = None) -> None:
        for binding in bindings:
            declaration = self.declaration(binding)
            if binding.enabled and declaration.runtime is not None:
                self.validate_target(binding)
            values = (parameters or {}).get(binding.package_id, {}).get(binding.plugin_id, {})
            resolved = (self.private_inputs.resolve(values, package_id=binding.package_id, plugin_id=binding.plugin_id,
                        schema=declaration.contributes.configuration)
                        if workspace_id is None else
                        delivered.get(binding.package_id, {}).get(binding.plugin_id, {})
                        if delivered is not None else None)
            validate_plugin_parameters(declaration.contributes.configuration, values, resolved)
        self.skill_sources.validate_plugin_bindings(bindings, self.packages, workspace_id=workspace_id)

    async def apply(self, payload: dict[str, object], revision: str) -> None:
        self.validate_defaults(payload)
        await self.apply_scope(gateway_plugin_bindings(payload), revision=revision, workspace_id=None,
                               parameters=plugin_parameters(payload))
        await self.apply_observation()

    async def _observation(self, slot: PluginSlot, *, apply: bool = False) -> RuntimeObservationStatus:
        assert slot.receipt is not None and slot.base_url is not None and slot.token is not None
        url = slot.base_url + "/internal/plugin/observation"
        headers = {"X-Local-Token": slot.token}
        desired = ({"instance_id": slot.receipt.instance_id,
            **cast(dict[str, object], self.settings()["observation"])} if apply else None)
        if desired is not None:
            response = await self.client.post(url, headers=headers, json=desired)
        else:
            response = await self.client.get(url, headers=headers)
        response.raise_for_status()
        actual = RuntimeObservationStatus.model_validate(response.json())
        if actual.instance_id != slot.receipt.instance_id:
            raise ValueError("诊断回执不属于原插件实例")
        if desired is not None and (actual.applied_revision != desired["revision"]
            or actual.telemetry.model_dump() != desired["telemetry"]
            or actual.statistics.model_dump() != desired["statistics"]):
            raise ValueError("插件诊断回执没有采用已提交的设置与 revision")
        return actual

    async def apply_observation(self) -> None:
        # 只有配置提交／接管边界发送；逐实例结算，不回滚成功项或重启失败插件。
        for key in tuple(self.slots):
            async with self._locks.setdefault(key, asyncio.Lock()):
                slot = self.slots.get(key)
                if slot is None or slot.state != "ready" or slot.receipt is None:
                    continue
                try:
                    await self._observation(slot, apply=True)
                except (httpx.HTTPError, ValueError, RuntimeError) as error:
                    slot.observation_error = f"诊断应用失败: {error}"
                    logger.exception("插件诊断配置应用失败: slot=%s", key)
                else:
                    slot.observation_error = None

    async def observation_status(self, workspace_id: str | None) -> list[PluginRuntimeObservationDTO]:
        result = []
        for key in tuple(self.slots):
            async with self._locks.setdefault(key, asyncio.Lock()):
                slot = self.slots.get(key)
                if slot is None or slot.receipt is None or (
                    workspace_id is not None and slot.binding.workspace_id != workspace_id
                ):
                    continue
                actual, error = None, slot.observation_error
                try:
                    if slot.state != "ready":
                        raise RuntimeError(f"插件诊断尚不可用: state={slot.state}")
                    actual = await self._observation(slot)
                except (httpx.HTTPError, ValueError, RuntimeError) as read_error:
                    error = f"诊断状态读取失败: {read_error}"
                result.append(PluginRuntimeObservationDTO(
                    instance=PluginInstanceView.model_validate(slot.receipt.model_dump(exclude={"log_sink"})),
                    observation=actual, error=error,
                ))
        return result

    async def apply_scope(self, entries: list[PluginBinding], *, revision: str, workspace_id: str | None,
                          parameters: PluginParameters | None = None) -> None:
        bindings = {self.key(binding): binding for binding in entries}
        if len(bindings) != len(entries) or any(binding.workspace_id != workspace_id for binding in entries):
            raise ValueError("插件绑定必须唯一且属于当前配置 owner 的范围")
        # 配置已经提交；启动失败只改实际回执，不回滚 enabled。
        for key, slot in tuple(self.slots.items()):
            if slot.binding.workspace_id == workspace_id and key not in bindings:
                async with self._locks.setdefault(key, asyncio.Lock()):
                    self.skill_sources.remove_plugin(key)
                    try:
                        await self._stop(key, slot)
                    except Exception as error:
                        slot.state, slot.error = "failed", str(error)
                        logger.exception("配置已删除，插件原实例尚未完成停止: slot=%s", key)
                    else:
                        del self.slots[key]
                    finally:
                        self._publish()
        for binding in bindings.values():
            await self.apply_binding(binding, revision=revision,
                parameters=(parameters or {}).get(binding.package_id, {}).get(binding.plugin_id, {}))

    async def apply_binding(self, binding: PluginBinding, *, revision: str, restart: bool = False,
                            parameters: dict[str, JsonValue],
                            delivered_parameters: dict[str, JsonValue] | None = None) -> dict[str, object]:
        key = self.key(binding)
        async with self._locks.setdefault(key, asyncio.Lock()):
            previous = self.slots.get(key)
            values = parameters
            parameters_revision = config_revision({"parameters": values})
            # revision 已由原配置 owner 核验；无关配置提交不要求给插件重复交接。
            if restart and (previous is None or previous.binding != binding):
                raise ValueError("重启基线已失效，请读取原绑定")
            if (previous is not None and previous.binding == binding
                    and previous.parameters_revision == parameters_revision and not restart):
                # 接管的 ready 实例在此恢复贡献；失败绑定不能借无关配置提交补发。
                if previous.state == "ready":
                    self.skill_sources.publish_plugin(binding, self.packages)
                previous.parameters = values
                previous.revision = revision
                self._publish()
                return previous.describe()
            declaration = self.declaration(binding)
            # 已有实例的显式重启／替换仍须先预检，不能先停旧实例再发现目标不兼容。
            if previous is not None and binding.enabled and declaration.runtime is not None:
                self.validate_target(binding)
            if restart and (not binding.enabled or declaration.runtime is None):
                raise ValueError("只有已开启的代码插件可以重启")
            if previous is not None:
                if previous.binding != binding:
                    self.skill_sources.remove_plugin(key)
                previous_version = previous.process_version or previous.binding.package_version
                try:
                    await self._stop(key, previous)
                except Exception as error:
                    # 期望已提交，实际句柄／回执仍属于旧实例；同一输入不得再次尝试替换。
                    slot = replace(previous, binding=binding, revision=revision, declaration=declaration,
                        parameters=values, state="failed", error=f"停止原实例失败（原运行版本 {previous_version}；未启动新实例）: {error}", commands={})
                    self.slots[key] = slot
                    logger.exception("插件旧实例停止失败，拒绝并行启动: slot=%s", key)
                    self._publish()
                    return slot.describe()
            slot = PluginSlot(binding, revision, declaration, self.gateway_id, parameters=values,
                package_digest=self.packages.get(binding.package_id, binding.package_version).digest,
                log_id=previous.log_id if previous is not None and not binding.enabled else None)
            self.slots[key] = slot
            stage = "启动实例" if binding.enabled and declaration.runtime is not None else "发布贡献"
            try:
                if binding.enabled and declaration.runtime is not None:
                    # 冷恢复的失败属于这个绑定，不阻断 Gateway 或其它插件。
                    if previous is None:
                        self.validate_target(binding)
                    await self._start(key, slot, delivered_parameters=delivered_parameters)
                stage = "发布贡献"
                self.skill_sources.publish_plugin(binding, self.packages)
            except asyncio.CancelledError:
                slot.state, slot.error = "failed", f"{stage}被取消；需显式重启"
                raise
            except BaseExceptionGroup as error:
                slot.state, slot.error = "failed", f"{stage}失败: {error}"
                logger.exception("插件启动及清理失败: slot=%s", key)
                raise
            except Exception as error:
                slot.state, slot.error = "failed", f"{stage}失败: {error}"
                logger.exception("插件启动或贡献发布失败: slot=%s", key)
            finally:
                self._publish()
            return slot.describe()

    def _record(self, key: Slot) -> Path:
        # 标识编码固定，包版本不参与槽位或进程发现身份。
        from hashlib import sha256

        identity = sha256(json.dumps([self.gateway_id, *key]).encode()).hexdigest()
        return self.directory / f"{identity}.json"

    def _check_receipt(self, slot: PluginSlot, receipt: PluginInstanceStatus, instance_id: str) -> None:
        binding, plugin = slot.binding, slot.declaration
        expected = (self.gateway_id, binding.package_id, binding.package_version, binding.plugin_id, plugin.instance_scope,
                    binding.workspace_id, instance_id, 1, 1, "ready")
        actual = (receipt.gateway_id, receipt.package_id, receipt.package_version, receipt.plugin_id, receipt.instance_scope,
                  receipt.workspace_id, receipt.instance_id, receipt.management_protocol_version,
                  receipt.extension_contract_version, receipt.status)
        if actual != expected or [service.model_dump() for service in receipt.services] != [
            service.model_dump() for service in plugin.contributes.services
        ]:
            raise ValueError("插件 ready 身份、合同或服务清单与精确绑定不符")
        missing = {"status", "shutdown", "observation"} - set(receipt.capabilities)
        if missing:
            raise ValueError("插件缺少必需管理能力: " + ", ".join(sorted(missing)))

    async def _start(
        self, key: Slot, slot: PluginSlot, *,
        delivered_parameters: dict[str, JsonValue] | None = None,
    ) -> None:
        record_path = self._record(key)
        if record_path.exists():
            raise RuntimeError("原插件进程发现记录尚未接管，拒绝重复启动；需重启所属运行时")
        instance_id, token = uuid4().hex, secrets.token_urlsafe(32)
        port = allocate_local_port()
        slot.state, slot.base_url, slot.token = "starting", f"http://127.0.0.1:{port}", token
        self._publish()
        environment = _local_child_environment()
        environment["BOXTEAM_PROJECT_ROOT"] = str(get_project_root())
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        environment["BOXTEAM_RUNTIME_CONTROL_TOKEN"] = token
        environment["BOXTEAM_PLUGIN_PORT"] = str(port)
        if slot.binding.workspace_id is not None:
            environment["WORKSPACE_ROOT"] = str(self.workspace_root(slot.binding.workspace_id))
            environment["BOXTEAM_WORKSPACE_ID"] = slot.binding.workspace_id
        environment["BOXTEAM_PLUGIN_INSTANCE"] = json.dumps({
            **slot.binding.model_dump(exclude={"enabled"}), "gateway_id": self.gateway_id,
            "instance_scope": slot.declaration.instance_scope,
            "instance_id": instance_id, "management_protocol_version": 1, "extension_contract_version": 1,
            "services": [service.model_dump() for service in slot.declaration.contributes.services]})
        environment["BOXTEAM_PLUGIN_COMMANDS"] = json.dumps([
            command.model_dump(mode="json") for command in slot.declaration.contributes.commands
        ])
        values = slot.parameters
        if slot.binding.workspace_id is None:
            values = self.private_inputs.resolve(values, package_id=slot.binding.package_id,
                plugin_id=slot.binding.plugin_id, schema=slot.declaration.contributes.configuration)
        elif requires_private_delivery(values, slot.declaration.contributes.configuration):
            if delivered_parameters is not None:
                values = delivered_parameters
            else:
                if self.workspace_parameters is None:
                    raise RuntimeError("Workspace 插件私有参数尚未接入原配置 owner")
                values = await self.workspace_parameters(slot.binding, slot.revision)
        validate_plugin_parameters(slot.declaration.contributes.configuration, slot.parameters, values)
        environment["BOXTEAM_PLUGIN_PARAMETERS"] = json.dumps(values, ensure_ascii=False)
        environment["BOXTEAM_RUNTIME_LOGGING"] = json.dumps(self.settings()["logging"])
        environment["BOXTEAM_RUNTIME_OBSERVATION"] = json.dumps({"instance_id": instance_id,
            **cast(dict[str, object], self.settings()["observation"])})
        root = self.packages.package_root(slot.binding.package_id, slot.binding.package_version)
        python_executable = resolve_python_executable()
        working_directory = self.directory / instance_id
        working_directory.mkdir(parents=True, exist_ok=True)
        entrypoint = slot.declaration.entrypoint
        assert entrypoint is not None
        if slot.declaration.runtime == "python":
            executable = (
                native_process_path(python_executable) if os.name == "nt" else str(python_executable)
            )
            site_packages = self.packages.python_site_packages(
                slot.binding.package_id, slot.binding.package_version, plugin_id=slot.binding.plugin_id,
            )
            environment.pop("PYTHONPATH", None)
            if site_packages is not None:
                environment["PYTHONPATH"] = (
                    native_process_path(site_packages) if os.name == "nt" else str(site_packages)
                )
            environment["PYTHONNOUSERSITE"] = "1"
        else:
            node_executable = os.environ.get("BOXTEAM_NODE_BIN")
            if not node_executable or not Path(node_executable).is_absolute():
                raise ValueError("Node 插件需要已配置的绝对 BOXTEAM_NODE_BIN，不临时发现或下载")
            executable = node_executable or ""
            environment.pop("NODE_PATH", None)
        log_file = self.logs.open("service-plugin", instance_id)
        slot.log_id = runtime_log_id(self.logs.path_for("service-plugin", instance_id))
        script_path = root / entrypoint[0]
        if slot.declaration.runtime == "python" and os.name == "nt":
            script_path = Path(native_process_path(script_path))
        process = _spawn_logged_process([executable, str(script_path), *entrypoint[1:]],
            log_file=log_file, cwd=working_directory, env=environment, detached=True)
        del environment["BOXTEAM_PLUGIN_PARAMETERS"]
        slot.process = ManagedProcess(process, log_file)
        slot.process_version = slot.binding.package_version
        deadline = time.monotonic() + 10
        try:
            while True:
                if not slot.process.is_alive():
                    raise RuntimeError(f"插件在 ready 前退出: exit_code={process.poll()}")
                try:
                    response = await self.client.get(slot.base_url + "/internal/plugin/status",
                        headers={"X-Local-Token": token}, timeout=max(0.01, deadline - time.monotonic()))
                except httpx.ConnectError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError("插件未在启动期限内 ready") from None
                    await asyncio.sleep(0.025)
                    continue
                response.raise_for_status()
                receipt = PluginInstanceStatus.model_validate(response.json())
                self._check_receipt(slot, receipt, instance_id)
                slot.commands = await self._command_registrations(slot, instance_id, slot.base_url, token)
                slot.process.confirm_service_identity(receipt.process_id)
                slot.receipt, slot.state = receipt, "ready"
                record_path.parent.mkdir(parents=True, exist_ok=True)
                temporary = record_path.with_suffix(".tmp")
                with temporary.open("w", encoding="utf-8") as stream:
                    restrict_private_path_access(temporary, descriptor=stream.fileno())
                    stream.write(_ProcessRecord(binding=slot.binding, revision=slot.revision, parameters_revision=slot.parameters_revision,
                        process_id=receipt.process_id, created_at=psutil.Process(receipt.process_id).create_time(),
                        instance_id=instance_id, base_url=slot.base_url, token=token).model_dump_json())
                temporary.replace(record_path)
                self._watch_exit(slot)
                return
        except BaseException as error:
            try:
                await run_cleanup_shielded(lambda: self._stop(key, slot))
            except Exception as cleanup_error:  # noqa: BLE001 — 保留启动与回收两个原始失败。
                raise BaseExceptionGroup(
                    f"插件启动失败: {error}；原进程回收失败: {cleanup_error}；目标进程未结清",
                    [error, cleanup_error]) from None
            raise

    async def _adopt(self, slot: PluginSlot, record: _ProcessRecord) -> bool:
        address = urlsplit(record.base_url)
        if (address.scheme != "http" or address.hostname != "127.0.0.1" or address.port is None
            or address.username or address.password or address.path not in {"", "/"}
            or address.query or address.fragment or record.binding != slot.binding):
            raise ValueError("插件接管记录不属于精确本机实例")
        process = AdoptedManagedProcess(record.process_id)
        if not process.is_alive():
            return False
        if psutil.Process(record.process_id).create_time() != record.created_at:
            raise RuntimeError("插件原 PID 已复用，拒绝接管其它进程")
        response = await self.client.get(record.base_url + "/internal/plugin/status",
            headers={"X-Local-Token": record.token})
        response.raise_for_status()
        receipt = PluginInstanceStatus.model_validate(response.json())
        self._check_receipt(slot, receipt, record.instance_id)
        slot.commands = await self._command_registrations(slot, record.instance_id, record.base_url, record.token)
        if receipt.process_id != record.process_id:
            raise ValueError("插件回执 PID 与原创建进程不符")
        ownership = self.logs.adopt("service-plugin", receipt.instance_id,
            {"pid": receipt.process_id, "log_sink": receipt.log_sink})
        slot.log_id = runtime_log_id(ownership.path)
        slot.process = AdoptedManagedProcess(receipt.process_id, ownership)
        slot.process_version = record.binding.package_version
        slot.receipt, slot.base_url, slot.token, slot.state = receipt, record.base_url, record.token, "ready"
        self._watch_exit(slot)
        return True

    async def _command_registrations(self, slot: PluginSlot, instance_id: str, base_url: str,
                                     token: str) -> dict[str, PluginCommandRegistration]:
        if not slot.declaration.contributes.commands:
            return {}
        response = await self.client.get(base_url + "/internal/plugin/commands", headers={"X-Local-Token": token})
        response.raise_for_status()
        registered = PluginCommandRegistrationsDTO.model_validate(response.json())
        expected = {command.id: command.handler for command in slot.declaration.contributes.commands}
        if (registered.instance_id != instance_id or len(registered.commands) != len(expected)
            or {item.command_id: item.handler for item in registered.commands} != expected
            or len({item.registration_id for item in registered.commands}) != len(expected)):
            raise ValueError("插件命令实际注册与精确清单或实例不符")
        return {item.command_id: item for item in registered.commands}

    async def adopt(self) -> None:
        """只恢复原 owner 交付的进程句柄，之后按当前配置处理停用或移除。"""
        if not self.directory.exists():
            return
        adopted: dict[Slot, PluginSlot] = {}
        try:
            for path in self.directory.glob("*.json"):
                record = _ProcessRecord.model_validate_json(path.read_text(encoding="utf-8"))
                key = self.key(record.binding)
                if path != self._record(key):
                    raise ValueError("插件进程发现记录与原槽位不符")
                slot = PluginSlot(record.binding, record.revision, self.declaration(record.binding), self.gateway_id,
                    package_digest=self.packages.get(record.binding.package_id, record.binding.package_version).digest)
                slot.parameters_revision = record.parameters_revision
                if await self._adopt(slot, record):
                    adopted[key] = slot
                else:
                    path.unlink()
        except BaseException:
            # 候选未完成接管，不关闭旧 Gateway 仍持有的实例。
            for slot in adopted.values():
                await self._release_exit_waiter(slot)
                assert slot.process is not None
                slot.process.detach()
            raise
        self.slots.update(adopted)
        self._publish()

    async def _stop(self, key: Slot, slot: PluginSlot) -> None:
        closing = asyncio.create_task(self._stop_process(key, slot))
        try:
            await asyncio.shield(closing)
        except asyncio.CancelledError:
            async def settle() -> None:
                await closing

            await run_cleanup_shielded(settle)
            raise

    async def _stop_process(self, key: Slot, slot: PluginSlot) -> None:
        # 先撤销新连接和原流准入，再进入同一进程关闭期限。
        slot.stopped.set()
        if slot.process is not None:
            slot.state = "stopping"
        await self._release_exit_waiter(slot)
        self._publish()
        if slot.process is not None:
            deadline = time.monotonic() + 8
            shutdown_error: httpx.HTTPError | TimeoutError | None = None
            if slot.process.is_alive() and slot.receipt is not None:
                try:
                    # 确认响应占剩余预算的一半，持续发送正文也不能挤掉实际进程回收。
                    async with asyncio.timeout(max(0, (deadline - time.monotonic()) / 2)):
                        response = await self.client.post(str(slot.base_url) + "/internal/plugin/shutdown",
                            headers={"X-Local-Token": str(slot.token)}, timeout=None)
                        response.raise_for_status()
                except (httpx.HTTPError, TimeoutError) as error:
                    shutdown_error = error
            # 原 close 在优雅退出失败后还会核强制回收；两段共享剩余期限。
            await run_sync_shielded(slot.process.close, timeout_seconds=max(0, (deadline - time.monotonic()) / 2))
            slot.process = None
            slot.process_version = None
            if shutdown_error is not None:
                self._record(key).unlink(missing_ok=True)
                slot.state = "stopped"
                raise RuntimeError("插件 shutdown 回执失败；原进程已核实退出") from shutdown_error
            self._record(key).unlink(missing_ok=True)
        slot.state = "stopped"
        self._publish()

    def service(self, binding: PluginBinding, *, service_id: str, instance_id: str,
                websocket: bool = False) -> tuple[str, str, asyncio.Event, str]:
        slot = self.slots.get(self.key(binding))
        if slot is None or slot.binding != binding:
            raise LookupError("插件绑定不可用或版本已切换")
        if not binding.enabled or slot.state != "ready" or slot.receipt is None:
            raise LookupError(f"插件尚未就绪: state={slot.state}, error={slot.error}")
        if slot.receipt.instance_id != instance_id:
            raise LookupError("插件实例已失效，不改投同名新实例")
        service = next((service for service in slot.receipt.services if service.service_id == service_id), None)
        if service is None:
            raise LookupError("插件未导出此服务")
        if websocket != (service.protocol in {"websocket", "dap"}):
            raise LookupError("连接传输与插件声明的服务协议不符")
        assert slot.base_url is not None and slot.token is not None
        return slot.base_url + "/services/" + service_id, slot.token, slot.stopped, slot.package_digest

    def references(self, package_id: str, version: str) -> list[str]:
        return [
            f"runtime:{slot.binding.plugin_id}:{slot.binding.workspace_id or 'gateway'}"
            for slot in self.slots.values()
            if slot.process is not None and slot.binding.package_id == package_id
            and slot.process_version == version]

    async def close(self, *, preserve: bool = False) -> None:
        errors: list[Exception] = []
        for key, slot in self.slots.items():
            try:
                if preserve:
                    await self._release_exit_waiter(slot)
                    if slot.process is not None:
                        slot.process.detach()
                else:
                    await self._stop(key, slot)
            except Exception as error:  # noqa: BLE001 — 所有进程都须尝试回收，结尾汇总原错误。
                errors.append(error)
        await self.client.aclose()
        if errors:
            raise ExceptionGroup("插件完整停止未完成", errors)


@asynccontextmanager
async def plugin_runtime_lifespan(*, home: Path, gateway_id: str,
    payload: Callable[[], dict[str, object]], revision: Callable[[], str],
    preserve: Callable[[], bool], workspace_root: Callable[[str], Path],
    skill_sources: GatewaySkillSourceOwner,
) -> AsyncIterator[GatewayPluginRuntime]:
    packages = PluginPackageStore(home / "plugins/packages")
    owner = GatewayPluginRuntime(home=home, gateway_id=gateway_id, packages=packages, workspace_root=workspace_root,
        skill_sources=skill_sources,
        settings=lambda: {"logging": {"revision": revision(),
            **cast(dict[str, dict[str, object]], payload()["logging"])["runtime"]},
            "observation": {"revision": revision(), **{name: cast(dict[str, object], payload()[name])["runtime"]
                for name in ("telemetry", "statistics")}}})
    try:
        yield owner
    finally:
        await owner.close(preserve=preserve())
