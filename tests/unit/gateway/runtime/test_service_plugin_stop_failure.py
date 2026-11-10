"""停止失败保留实际句柄，并独立报告已提交的绑定期望。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from unittest.mock import Mock

import httpx
import pytest

from app.gateway.plugins.packages import PluginPackageStore
from app.gateway.runtime.process_lifecycle import ManagedProcess
from app.gateway.runtime.service_plugins.runtime import GatewayPluginRuntime, PluginSlot
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
                "requires": [{"extension_point": "services", "version": 1}],
                "contributes": {"services": [{"service_id": "echo", "service_type": "terminal", "protocol": "http"}]}}],
        }))
        (source / "entry.py").write_text("# 本例只替换进程边界。\n")
        packages.install_directory(source)
    skills = GatewaySkillSourceOwner(gateway_id="gateway_" + "a" * 32, skills_root=tmp_path / "skills")
    await skills.start()
    owner = GatewayPluginRuntime(home=tmp_path / "home", gateway_id=skills.gateway_id,
        packages=packages, settings=dict, workspace_root=lambda _: tmp_path, skill_sources=skills)
    await owner.client.aclose()
    owner.client = httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200)))
    processes: list[Mock] = []

    async def start(_key, slot: PluginSlot, *, delivered_parameters=None) -> None:
        assert delivered_parameters is None
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
    assert bool(owner.references("stop.probe", "2.0.0")) == (operation == "switch")
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
        async def fail_start(_key, _slot, *, delivered_parameters=None):
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

    async def fail_after_cleanup(_key, slot: PluginSlot, *, delivered_parameters=None) -> None:
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
