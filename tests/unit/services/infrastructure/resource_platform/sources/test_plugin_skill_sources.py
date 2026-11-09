"""插件贡献复用原 Skill owner 的隔离、冲突和真实文件读取引用。"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from threading import Event

import pytest
from pydantic import JsonValue

from app.gateway.plugins.packages import PluginPackageStore
from app.gateway.runtime.service_plugins.runtime import (
    GatewayPluginRuntime,
    _ProcessRecord,
)
from app.gateway.runtime.skill_sources import GatewaySkillSourceOwner
from app.schemas.gateway.service_plugin import PluginBinding
from app.schemas.internal_v2.resource_navigation import ResourceNavigationRequest
from app.services.infrastructure.resource_platform.sources.resource_navigation import (
    ResourceNavigation,
)
from app.services.infrastructure.resource_platform.virtual_resources.resolver import (
    ResourceUriResolveError,
)

GATEWAY = "gateway_" + "a" * 32
WORKSPACE_A = "11111111-1111-4111-8111-111111111111"
WORKSPACE_B = "22222222-2222-4222-8222-222222222222"


@dataclass
class SkillCase:
    owner: GatewaySkillSourceOwner
    runtime: GatewayPluginRuntime
    packages: PluginPackageStore
    root: Path

    def install(self, package: str, *, code: bool = False) -> PluginBinding:
        source = self.root / package
        (source / "guide").mkdir(parents=True)
        (source / "plugin.jsonc").write_text(json.dumps({
            "package_id": package, "version": "1.0.0", "name": package,
            "plugins": [{"id": "guide", "name": "Guide", "instance_scope": "workspace",
                **({"runtime": "python", "entrypoint": ["main.py"], "management_protocol_version": 1} if code else {}),
                "requires": [{"extension_point": "skills", "version": 1}],
                "contributes": {"skills": [{"id": "guide", "path": "guide"}]}}],
        }))
        (source / "guide/SKILL.md").write_text("---\nname: demo\ndescription: Demo guide\n---\nBODY\n")
        if code:
            (source / "main.py").write_text("# 测试仅替换进程边界，不执行此入口。\n")
        self.packages.install_directory(source)
        return PluginBinding(package_id=package, package_version="1.0.0", plugin_id="guide",
                             enabled=True, workspace_id=WORKSPACE_A)


@pytest.fixture
async def skill_case(tmp_path: Path):
    owner = GatewaySkillSourceOwner(gateway_id=GATEWAY, skills_root=tmp_path / "home/skills")
    await owner.start()
    packages = PluginPackageStore(tmp_path / "packages")
    runtime = GatewayPluginRuntime(home=tmp_path / "home", gateway_id=GATEWAY,
        packages=packages, workspace_root=lambda _: tmp_path, settings=dict, skill_sources=owner)
    try:
        yield SkillCase(owner, runtime, packages, tmp_path)
    finally:
        await runtime.close()
        await owner.close()


async def test_conflict_rejects_candidate_without_replacing_publication_and_unchanged_binding_does_no_io(
    skill_case: SkillCase, monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = skill_case
    first = case.install("proof.first")
    await case.runtime.apply_scope([first], revision="first", workspace_id=WORKSPACE_A)
    accepted = case.owner.snapshot(workspace_id=WORKSPACE_A)
    entry, = accepted.entries
    second = case.install("proof.second")
    with pytest.raises(ValueError, match="名称冲突"):
        case.runtime.validate_bindings([first, second], workspace_id=WORKSPACE_A)
    assert case.owner.snapshot(workspace_id=WORKSPACE_A) == accepted
    assert case.owner.snapshot(workspace_id=WORKSPACE_B).entries == ()
    sibling = second.model_copy(update={"workspace_id": WORKSPACE_B})
    await case.runtime.apply_scope([sibling], revision="sibling", workspace_id=WORKSPACE_B)
    assert case.owner.snapshot(workspace_id=WORKSPACE_B).entries[0].display_uri != entry.display_uri

    def unexpected_read(*_args, **_kwargs):
        raise AssertionError("无关配置或暖读取不得重读、重算 Skill 正文")

    monkeypatch.setattr(case.owner, "_plugin_candidate", unexpected_read)
    await case.runtime.apply_scope([first], revision="unrelated-config", workspace_id=WORKSPACE_A)
    assert case.owner.snapshot(workspace_id=WORKSPACE_A) == accepted


async def test_code_restart_preserves_static_skill_publication(
    skill_case: SkillCase, monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = skill_case
    binding = case.install("proof.restart", code=True)

    async def started(_key, slot):
        slot.state = "ready"

    monkeypatch.setattr(case.runtime, "_start", started)
    await case.runtime.apply_scope([binding], revision="first", workspace_id=WORKSPACE_A)
    accepted = case.owner.snapshot(workspace_id=WORKSPACE_A)
    uri = accepted.entries[0].display_uri

    async def stopped(_key, slot):
        # 旧进程停止期间说明仍可读取；显式重启不能使静态来源短暂失效。
        assert case.owner.snapshot(workspace_id=WORKSPACE_A) == accepted
        result = await case.owner.navigate(ResourceNavigationRequest(uri=uri, operation="read"),
                                           workspace_id=WORKSPACE_A)
        assert result.content is not None and "BODY" in result.content
        slot.state = "stopped"

    monkeypatch.setattr(case.runtime, "_stop", stopped)
    status = await case.runtime.apply_binding(binding, revision="restart", restart=True, parameters={})
    assert status["state"] == "ready"
    assert case.owner.snapshot(workspace_id=WORKSPACE_A) == accepted


async def test_adopted_parameters_are_rehydrated_from_owner_without_restarting(
    skill_case: SkillCase, monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = skill_case
    binding = case.install("proof.adopt", code=True)
    from app.core.config_sources import config_revision

    values: dict[str, JsonValue] = {"label": "owner-only-value"}
    record = _ProcessRecord(binding=binding, revision="before-handoff",
        parameters_revision=config_revision({"parameters": values}), process_id=1,
        created_at=1.0, instance_id="same-instance", base_url="http://127.0.0.1:12345", token="private")
    path = case.runtime._record(case.runtime.key(binding))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(record.model_dump_json())
    assert "owner-only-value" not in path.read_text()

    async def adopted(slot, _record):
        slot.state = "ready"
        return True

    async def unexpected_start(*_args):
        pytest.fail("同参数的原 owner 交付不应重启已接管实例")

    monkeypatch.setattr(case.runtime, "_adopt", adopted)
    monkeypatch.setattr(case.runtime, "_start", unexpected_start)
    await case.runtime.adopt()
    original = case.runtime.slots[case.runtime.key(binding)]
    assert original.parameters == {}
    # 本例只验证接管表示与交付比较；schema 候选已由真实产品参数用例覆盖。
    await case.runtime.apply_binding(binding, revision="after-handoff", parameters=values)
    assert case.runtime.slots[case.runtime.key(binding)] is original
    assert original.parameters == values and original.revision == "after-handoff"


async def test_cancelled_read_holds_package_until_real_io_finishes_and_disable_revokes_result(
    skill_case: SkillCase, monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = skill_case
    binding = case.install("proof.read")
    await case.runtime.apply_scope([binding], revision="first", workspace_id=WORKSPACE_A)
    uri = case.owner.snapshot(workspace_id=WORKSPACE_A).entries[0].display_uri
    entered, release = Event(), Event()
    original = ResourceNavigation.navigate_sync

    def blocked_read(self, *args, **kwargs):
        entered.set()
        if not release.wait(3):
            raise TimeoutError("测试没有释放实际文件读取")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(ResourceNavigation, "navigate_sync", blocked_read)
    task = asyncio.create_task(case.owner.navigate(ResourceNavigationRequest(uri=uri, operation="read"),
                                                   workspace_id=WORKSPACE_A))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        assert case.owner.read_references("proof.read", "1.0.0")
        task.cancel()
        await case.runtime.apply_scope([binding.model_copy(update={"enabled": False})],
                                       revision="disabled", workspace_id=WORKSPACE_A)
        assert case.owner.snapshot(workspace_id=WORKSPACE_A).entries == ()
        assert case.owner.read_references("proof.read", "1.0.0")
        with pytest.raises(ResourceUriResolveError, match="未开启"):
            await case.owner.navigate(ResourceNavigationRequest(uri=uri, operation="read"), workspace_id=WORKSPACE_A)
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert case.owner.read_references("proof.read", "1.0.0") == []
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
