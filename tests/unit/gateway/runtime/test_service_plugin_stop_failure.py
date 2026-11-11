"""停止失败保留实际句柄，并独立报告已提交的绑定期望。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from unittest.mock import Mock

import httpx
import pytest
from pydantic import JsonValue

from app.gateway.plugins.packages import PluginPackageStore
from app.gateway.runtime.process_lifecycle import ManagedProcess
from app.gateway.runtime.service_plugins.runtime import (
    GatewayPluginRuntime,
    PluginSlot,
    _ProcessRecord,
)
from app.gateway.runtime.skill_sources import GatewaySkillSourceOwner
from app.schemas.gateway.service_plugin import (
    PluginBinding,
    PluginInstanceStatus,
    PluginSlotStatusDTO,
)
from tests.support.plugin_package import native_plugin_target


@pytest.fixture
async def blocked_plugin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[tuple[GatewayPluginRuntime, list[Mock], PluginBinding]]:
    packages = PluginPackageStore(tmp_path / "packages")
    for version in ("1.0.0", "2.0.0"):
        source = tmp_path / version
        source.mkdir()
        (source / "plugin.jsonc").write_text(json.dumps({
            "package_id": "stop.probe", "version": version, "name": "Stop probe",
            "target": native_plugin_target("python"),
            "plugins": [{"id": "probe", "name": "Probe", "instance_scope": "gateway",
                "runtime": "python", "entrypoint": ["entry.py"], "management_protocol_version": 1,
                "requires": [{"extension_point": "services", "version": 1},
                    {"extension_point": "configuration", "version": 1}],
                "contributes": {"configuration": {"type": "object", "additionalProperties": False}, "services": [{"service_id": "echo", "service_type": "terminal", "protocol": "http"}]}}],
        }))
        (source / "entry.py").write_text("# 本例只替换进程边界。\n")
        packages.install_directory(source)
    skills = GatewaySkillSourceOwner(gateway_id="gateway_" + "a" * 32, skills_root=tmp_path / "skills")
    await skills.start()
    owner = GatewayPluginRuntime(home=tmp_path / "home", gateway_id=skills.gateway_id,
        packages=packages, settings=dict, workspace_root=lambda _: tmp_path, skill_sources=skills)
    await owner.client.aclose()
    processes: list[Mock] = []

    def shutdown(request: httpx.Request) -> httpx.Response:
        slot, = (slot for slot in owner.slots.values() if slot.receipt is not None
            and slot.base_url is not None and slot.base_url + "/internal/plugin/shutdown" == str(request.url))
        assert slot.receipt is not None
        return httpx.Response(200, json={"accepted": True, "instance_id": slot.receipt.instance_id,
            "process_id": slot.receipt.process_id})

    owner.client = httpx.AsyncClient(transport=httpx.MockTransport(shutdown))

    async def start(_key, slot: PluginSlot, *, parameters) -> None:
        assert parameters == {}
        assert slot.declaration is not None
        process = Mock(spec=ManagedProcess)
        process.is_alive.return_value = True
        processes.append(process)
        slot.process, slot.process_version = process, slot.binding.package_version
        slot.base_url, slot.token, slot.state = "http://127.0.0.1:12345", "private", "ready"
        slot.receipt = PluginInstanceStatus(gateway_id=owner.gateway_id,
            **slot.binding.model_dump(exclude={"enabled"}), instance_scope="gateway",
            instance_id=f"instance-{len(processes)}", process_id=len(processes), status="ready",
            management_protocol_version=1, extension_contract_version=1,
            services=slot.declaration.contributes.services)

    monkeypatch.setattr(owner, "_start", start)
    binding = PluginBinding(package_id="stop.probe", package_version="1.0.0", plugin_id="probe", enabled=True)
    await owner.apply_binding(binding, revision="v1", parameters={})
    processes[0].close.side_effect = TimeoutError("原进程仍未退出")
    try:
        yield owner, processes, binding
    finally:
        for process in processes:
            process.close.side_effect = None
        await owner.close()
        await skills.close()


@pytest.mark.parametrize("workspace_id", [None, "workspace-owner"])
@pytest.mark.parametrize("operation", ["disable", "new-disabled", "switch-disabled"])
async def test_disabled_candidate_checks_compatibility_except_when_closing_original_binding(
    blocked_plugin: tuple[GatewayPluginRuntime, list[Mock], PluginBinding],
    monkeypatch: pytest.MonkeyPatch, workspace_id: str | None, operation: str,
) -> None:
    owner, processes, original = blocked_plugin
    previous = owner.slots.pop(owner.key(original))
    previous.binding = original.model_copy(update={"workspace_id": workspace_id})
    assert previous.declaration is not None
    previous.declaration = previous.declaration.model_copy(update={
        "instance_scope": "workspace" if workspace_id else "gateway"})
    owner.slots[owner.key(previous.binding)] = previous
    desired = previous.binding.model_copy(update={"enabled": False,
        "package_version": "2.0.0" if operation == "switch-disabled" else "1.0.0"})
    if operation == "new-disabled":
        owner.slots.clear()
    monkeypatch.setattr(owner, "declaration", lambda _binding: previous.declaration)
    validate = Mock(side_effect=ValueError("插件目标 Node ABI 不匹配"))
    monkeypatch.setattr(owner, "validate_target", validate)
    try:
        if operation == "disable":
            owner.validate_bindings([desired], workspace_id=workspace_id)
            validate.assert_not_called()
        else:
            with pytest.raises(ValueError, match="Node ABI 不匹配"):
                owner.validate_bindings([desired], workspace_id=workspace_id)
            validate.assert_called_once_with(desired)
        processes[0].close.assert_not_called()
        assert previous.binding.enabled and previous.binding.package_version == "1.0.0"
    finally:
        # fixture 仍负责释放其实际句柄；候选校验没有触碰已持有进程。
        owner.slots.clear()
        previous.binding = original
        owner.slots[owner.key(original)] = previous


@pytest.mark.parametrize("operation", ["disable", "switch"])
@pytest.mark.parametrize("ready_receipt", [False, True], ids=["before-ready", "after-ready"])
async def test_failed_stop_keeps_actual_version_and_does_not_retry_unchanged_desired_binding(
    blocked_plugin: tuple[GatewayPluginRuntime, list[Mock], PluginBinding], operation: str, ready_receipt: bool,
) -> None:
    owner, processes, original = blocked_plugin
    previous = owner.slots[owner.key(original)]
    if not ready_receipt:
        # 启动未取得 ready，且原进程回收也失败时，仍须保留其包版本。
        previous.receipt, previous.state = None, "failed"
    actual = previous.receipt.model_dump(mode="json", exclude={"log_sink"}) if previous.receipt else None
    desired = original.model_copy(update={"enabled": False} if operation == "disable" else {"package_version": "2.0.0"})
    result = await owner.apply_binding(desired, revision="committed", parameters={})
    assert result["state"] == "failed" and result["binding"] == desired.model_dump(mode="json")
    assert result["instance"] == actual
    assert result["error"] == "停止原实例失败（原运行版本 1.0.0；未启动新实例）: 原进程仍未退出" and result["revision"] == "committed"
    current = owner.slots[owner.key(desired)]
    assert current.process is processes[0] and current.stopped.is_set()
    assert owner.references("stop.probe", "1.0.0"), "旧进程未结清仍保护原版本"
    assert owner.references("stop.probe", "2.0.0") == [], "期望版本由配置引用保护，运行引用只保护实际未结清的进程"
    with pytest.raises(LookupError):
        owner.service(original, service_id="echo", instance_id="instance-1")
    with pytest.raises(LookupError):
        owner.service(desired, service_id="echo", instance_id="instance-1")
    unchanged = await owner.apply_binding(desired, revision="unrelated", parameters={})
    assert unchanged["state"] == "failed" and unchanged["instance"] == result["instance"]
    assert len(processes) == 1 and processes[0].close.call_count == 1
    if operation == "disable":
        with pytest.raises(ValueError):
            await owner.apply_binding(desired, revision="unrelated", restart=True, parameters={})
        assert processes[0].close.call_count == 1
    else:
        processes[0].close.side_effect = None
        restarted = PluginSlotStatusDTO.model_validate(await owner.apply_binding(
            desired, revision="explicit", restart=True, parameters={}))
        assert restarted.state == "ready" and restarted.instance is not None
        assert restarted.instance.package_version == "2.0.0"
        assert restarted.instance.instance_id == "instance-2" and len(processes) == 2
        assert owner.references("stop.probe", "1.0.0") == []


@pytest.mark.parametrize("stage", ["start", "publish"])
async def test_failed_replacement_reports_stage_and_does_not_restore_or_repeat(
    blocked_plugin: tuple[GatewayPluginRuntime, list[Mock], PluginBinding],
    monkeypatch: pytest.MonkeyPatch, stage: str,
) -> None:
    owner, processes, original = blocked_plugin
    processes[0].close.side_effect = None
    desired = original.model_copy(update={"package_version": "2.0.0"})
    if stage == "start":
        async def fail_start(_key, _slot, *, parameters):
            raise RuntimeError("新入口不可用")
        monkeypatch.setattr(owner, "_start", fail_start)
    else:
        monkeypatch.setattr(owner.skill_sources, "publish_plugin", Mock(side_effect=ValueError("Skill 来源冲突")))
    failed = PluginSlotStatusDTO.model_validate(await owner.apply_binding(desired, revision="changed", parameters={}))
    assert failed.binding == desired and failed.state == "failed"
    assert failed.error == ("启动实例失败: 新入口不可用" if stage == "start" else "发布贡献失败: Skill 来源冲突")
    assert processes[0].close.call_count == 1
    assert len(processes) == (1 if stage == "start" else 2)
    if stage == "start":
        assert failed.instance is None
    else:
        assert failed.instance is not None and failed.instance.package_version == "2.0.0"
        with pytest.raises(LookupError):
            owner.service(desired, service_id="echo", instance_id=failed.instance.instance_id)
    unchanged = await owner.apply_binding(desired, revision="unrelated", parameters={})
    assert unchanged["error"] == failed.error and len(processes) == (1 if stage == "start" else 2)


async def test_cancelled_start_with_cleanup_failure_publishes_failed_slot_and_preserves_group(
    blocked_plugin: tuple[GatewayPluginRuntime, list[Mock], PluginBinding], monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner, processes, original = blocked_plugin
    processes[0].close.side_effect = None
    desired = original.model_copy(update={"package_version": "2.0.0"})
    process = Mock(spec=ManagedProcess)
    process.is_alive.return_value = True
    cancellation = asyncio.CancelledError("启动任务取消")
    cleanup = RuntimeError("新进程未能回收")
    failure = BaseExceptionGroup(
        f"插件启动失败: {cancellation}；原进程回收失败: {cleanup}；目标进程未结清",
        [cancellation, cleanup],
    )

    async def fail_after_cleanup(_key, slot: PluginSlot, *, parameters) -> None:
        slot.process, slot.process_version = process, slot.binding.package_version
        raise failure

    monkeypatch.setattr(owner, "_start", fail_after_cleanup)
    with pytest.raises(BaseExceptionGroup) as caught:
        await owner.apply_binding(desired, revision="changed", parameters={})

    assert caught.value is failure
    assert caught.value.exceptions == (cancellation, cleanup)
    slot = owner.slots[owner.key(desired)]
    assert slot.binding == desired and slot.state == "failed"
    assert "启动任务取消" in (slot.error or "") and "新进程未能回收" in (slot.error or "")
    assert "目标进程未结清" in (slot.error or "")
    assert slot.process is process and slot.process_version == "2.0.0"

    unchanged = await owner.apply_binding(desired, revision="unrelated", parameters={})
    assert unchanged["state"] == "failed" and unchanged["error"] == slot.error
    assert unchanged["binding"] == desired.model_dump(mode="json")
    assert processes[0].close.call_count == 1 and process.close.call_count == 0


@pytest.mark.parametrize("failure", ["missing-package", "missing-plugin", "parameters"])
async def test_committed_binding_failure_retires_old_access_without_blocking_other_bindings(
    blocked_plugin: tuple[GatewayPluginRuntime, list[Mock], PluginBinding], failure: str,
) -> None:
    owner, processes, original = blocked_plugin
    desired = original.model_copy(update={
        "package_version": "9.0.0" if failure == "missing-package" else "2.0.0",
        "plugin_id": "missing" if failure == "missing-plugin" else original.plugin_id,
    })
    parameters: dict[str, JsonValue] = {"unexpected": "value"} if failure == "parameters" else {}
    previous = owner.slots[owner.key(original)]
    if failure == "missing-plugin":
        processes[0].close.side_effect = None
    await owner.apply_scope([desired], revision="committed", workspace_id=None,
        parameters={desired.package_id: {desired.plugin_id: parameters}})
    failed = PluginSlotStatusDTO.model_validate(owner.slots[owner.key(desired)].describe())
    assert failed.binding == desired and failed.revision == "committed" and failed.state == "failed"
    assert failed.error and "绑定不可用" in failed.error
    assert previous.stopped.is_set()
    assert len(processes) == 1
    with pytest.raises(LookupError):
        owner.service(original, service_id="echo", instance_id="instance-1")
    assert owner.command_snapshot(None).publications == []
    if failure != "missing-plugin":
        assert owner.references("stop.probe", "1.0.0"), "旧进程仍未结清时保护真实运行版本"
        assert processes[0].close.call_count == 0
    # 相同已提交输入不能借无关配置变化自动重试；独立绑定仍可正常发布。
    sibling = original.model_copy(update={"enabled": False}) if failure == "missing-plugin" else None
    repeated = await owner.apply_binding(desired, revision="unrelated", parameters=parameters)
    assert repeated["state"] == "failed" and repeated["error"] == failed.error
    if sibling is not None:
        assert (await owner.apply_binding(sibling, revision="sibling", parameters={}))['state'] == 'stopped'


@pytest.mark.parametrize("scope", [None, "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"])
async def test_missing_saved_package_keeps_failed_slot_and_recovers_remaining_scope(
    blocked_plugin: tuple[GatewayPluginRuntime, list[Mock], PluginBinding], scope: str | None, tmp_path: Path,
) -> None:
    owner, processes, original = blocked_plugin
    processes[0].close.side_effect = None
    source = tmp_path / "healthy"
    source.mkdir()
    (source / "plugin.jsonc").write_text(json.dumps({
        "package_id": "healthy.probe", "version": "1.0.0", "name": "Healthy probe",
        "plugins": [{"id": "settings", "name": "Settings", "instance_scope": "workspace" if scope else "gateway",
            "requires": [{"extension_point": "configuration", "version": 1}],
            "contributes": {"configuration": {"type": "object"}}}],
    }))
    owner.packages.install_directory(source)
    missing = original.model_copy(update={"package_id": "missing.probe", "workspace_id": scope})
    healthy = PluginBinding(package_id="healthy.probe", plugin_id="settings", package_version="1.0.0",
        enabled=False, workspace_id=scope)
    await owner.apply_scope([missing, healthy], revision="cold-recovery", workspace_id=scope)
    failed = PluginSlotStatusDTO.model_validate(owner.slots[owner.key(missing)].describe())
    assert failed.binding == missing and failed.state == "failed" and failed.instance is None
    assert failed.error and "missing.probe@1.0.0" in failed.error
    assert failed.instance_scope == ("workspace" if scope else "gateway")
    assert failed.declarative is False, "缺失声明不能伪称纯声明插件"
    assert owner.slots[owner.key(healthy)].describe()["state"] == "stopped"
    if scope is None:
        await owner.apply({"plugins": {"runtime": {"entries": [healthy.model_dump(mode="json")]},
            "workspace_defaults": {"entries": [missing.model_dump(mode="json")]}}}, revision="unrelated-template")
        assert owner.key(missing) not in owner.slots, "无运行绑定的模板不阻断应用启动或创建实例"
    assert owner.command_snapshot(scope).publications == []
    assert len(processes) == 1, "不因缺包恢复隐式创建进程"
    await owner.apply_scope([healthy], revision="unbound", workspace_id=scope)
    assert owner.key(missing) not in owner.slots


@pytest.mark.parametrize("process_state", ["exited", "reused-pid", "alive-missing-package"])
async def test_adoption_checks_process_identity_before_loading_saved_package(
    blocked_plugin: tuple[GatewayPluginRuntime, list[Mock], PluginBinding],
    monkeypatch: pytest.MonkeyPatch, process_state: str,
) -> None:
    owner, processes, original = blocked_plugin
    binding = original.model_copy(update={"package_id": "missing.probe"})
    record = _ProcessRecord(binding=binding, revision="saved", parameters_revision="saved-parameters",
        process_id=12345, created_at=1.0, instance_id="old-instance",
        base_url="http://127.0.0.1:12346", token="private")
    path = owner._record(owner.key(binding))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(record.model_dump_json(), encoding="utf-8")
    process = Mock()
    process.is_alive.return_value = process_state != "exited"
    monkeypatch.setattr("app.gateway.runtime.service_plugins.runtime.AdoptedManagedProcess", Mock(return_value=process))
    identity = Mock()
    identity.create_time.return_value = 2.0 if process_state == "reused-pid" else record.created_at
    inspect_identity = Mock(return_value=identity)
    monkeypatch.setattr("app.gateway.runtime.service_plugins.runtime.psutil.Process", inspect_identity)
    if process_state == "exited":
        await owner.adopt()
        assert not path.exists(), "已退出实例的旧发现记录不要求包仍存在"
        inspect_identity.assert_not_called()
    else:
        error = RuntimeError if process_state == "reused-pid" else FileNotFoundError
        with pytest.raises(error, match="PID 已复用" if process_state == "reused-pid" else "missing.probe"):
            await owner.adopt()
        assert path.exists(), "不能清理仍存活或身份不符的原发现记录"
    assert owner.key(binding) not in owner.slots
    assert owner.slots[owner.key(original)].state == "ready"
    assert len(processes) == 1 and processes[0].close.call_count == 0
