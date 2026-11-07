"""真实常驻安装器的 Web 准入：本次扫描复用，跨请求仍重验原件。"""

from __future__ import annotations

import json
import time

import pytest

from app.services.infrastructure.extension_host.management import (
    ExtensionManagementError,
)
from tests.integration.workspace_services.extension_host.test_extension_management import (
    _backend,
    management_backends,
    management_runtime,
    vsix_factory,
)

__all__ = ["management_backends", "management_runtime", "vsix_factory"]


async def test_web_admission_rechecks_profile_and_manifest_without_repeated_package_scans(
    management_backends, vsix_factory,
) -> None:
    case = management_backends
    registry = _backend(case.workspace / "scan-target", case)
    management = registry.extension_hosts.management
    installed = [await management.install(vsix_factory(case.workspace, "1.0.0", f"scan-{index}",
        manifest_overrides={"name": f"scan-{index}", "browser": "./extension.js"}, omit=("main",))) for index in range(4)]
    inventory = installed[-1].inventory
    keys = [entry.extension_key for entry in reversed(inventory.extensions)]
    started = time.time()
    publication = await management.web_publication(profile_id=inventory.profile_id, extension_keys=keys)
    completed = time.time()
    assert [entry.extension_key for entry in publication.inventory.extensions] == keys
    assert [entry.extension_key for entry in publication.publications] == keys
    assert all(entry.is_valid for entry in publication.inventory.extensions)
    assert publication.inventory.target_platform == inventory.target_platform
    process = case.domains[0].owner.instance
    assert process is not None
    (case.context.artifacts_dir / "admission-window.json").write_text(json.dumps({
        "started": started, "completed": completed, "process_id": process.process_id,
        "directory": str(case.domains[0].owner.directory), "extension_keys": keys,
    }, indent=2))

    for rejected_keys in ([], [keys[0], keys[0]], ["not-installed"]):
        with pytest.raises(ExtensionManagementError):
            await management.web_publication(profile_id=inventory.profile_id, extension_keys=rejected_keys)
    with pytest.raises(ExtensionManagementError):
        await management.web_publication(profile_id="another-profile", extension_keys=keys)

    directory = case.domains[0].owner.directory / "extensions"
    manifest = directory / keys[0] / "package.json"
    original = manifest.read_bytes()
    try:
        # 先有一次成功准入，随后损坏原件；不能以此前发布结果或 scanner 缓存掩盖。
        for content in (b"{broken", original.replace(b'"version": "1.0.0"', b'"version": "9.0.0"')):
            assert content != original
            manifest.write_bytes(content)
            with pytest.raises(ExtensionManagementError):
                await management.web_publication(profile_id=inventory.profile_id, extension_keys=keys)
        manifest.unlink()
        with pytest.raises(ExtensionManagementError):
            await management.web_publication(profile_id=inventory.profile_id, extension_keys=keys)
    finally:
        manifest.write_bytes(original)
    assert [entry.extension_key for entry in (await management.web_publication(
        profile_id=inventory.profile_id, extension_keys=keys)).inventory.extensions] == keys

    profile = directory / "extensions.json"
    original_profile = profile.read_bytes()
    members = json.loads(original_profile)
    try:
        profile.write_text(json.dumps([*members, members[0]]))
        with pytest.raises(ExtensionManagementError):
            await management.web_publication(profile_id=inventory.profile_id, extension_keys=keys)
    finally:
        profile.write_bytes(original_profile)

    original_package = directory / keys[0]
    for alternate in (directory / "unpublished-alias", case.workspace / "outside-installation"):
        original_package.rename(alternate)
        relocated = json.loads(original_profile)
        member = next(item for item in relocated if item["relativeLocation"] == keys[0])
        member.pop("relativeLocation")
        # 创建自洽 URI，不把旧 toJSON 的 external/fsPath 派生缓存复制到新 path。
        member["location"] = {"$mid": 1, "scheme": "file", "path": alternate.as_posix()}
        try:
            profile.write_text(json.dumps(relocated))
            # 有效 manifest 和 profile 成员也不能授权非发布目录。
            with pytest.raises(ExtensionManagementError):
                await management.web_publication(profile_id=inventory.profile_id, extension_keys=keys)
        finally:
            profile.write_bytes(original_profile)
            alternate.rename(original_package)
    assert [entry.extension_key for entry in (await management.web_publication(
        profile_id=inventory.profile_id, extension_keys=keys)).publications] == keys
