"""真实原图片编辑器经 Gateway 复用 HTTP 字节，缓存不绕过目标与授权。"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import shutil
import struct
import subprocess
import zlib
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.harness.python.run_context import TestRunContext
from tests.support.e2e_control.harness import E2EProcessLease
from tests.support.paths import chromium_temporary_environment
from tests.support.ports import e2e_port_block_for_file
from tests.support.processes.gateway import close_gateway_process, start_gateway_process
from tests.support.workspaces import (
    install_test_workspace_config,
    prepare_test_workspace,
)


@pytest.fixture(scope="module")
def e2e_workspace_root_path(request: pytest.FixtureRequest) -> str:
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    return str(prepare_test_workspace(
        workspace_root=context.output_root / "workspace",
        template_root=context.project_root / "tests/fixtures/workspaces/default_test_workspace",
        template_items=(".boxteam/workspace-identity.json",),
    ))


@pytest.fixture
def image_versions() -> tuple[bytes, bytes]:
    def png(width: int) -> bytes:
        def chunk(kind: bytes, data: bytes) -> bytes:
            return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

        return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, 1, 8, 2, 0, 0, 0))
                + chunk(b"IDAT", zlib.compress(b"\0" + b"\xff\0\0" * width)) + chunk(b"IEND", b""))

    return png(1), png(2)


@pytest.fixture
def cache_gateway_port(request: pytest.FixtureRequest) -> Iterator[int]:
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    port = e2e_port_block_for_file(Path(request.node.path)).port(20)
    lease = E2EProcessLease(context.runtime_root_for_node(request.node.nodeid) / "gateway-port.json", (port,))
    lease.acquire()
    try:
        yield port
    finally:
        lease.release()


async def test_original_image_editor_revalidates_private_file_cache(
    request: pytest.FixtureRequest, e2e_workspace_root_path: str, e2e_workspace_config_path: str,
    cache_gateway_port: int, web_workbench_assets: Path, image_versions: tuple[bytes, bytes],
) -> None:
    chromium = os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH") or shutil.which("chromium")
    assert chromium, "原图片缓存验收需要 Chromium"
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    immutable = json.loads((web_workbench_assets / "immutable-assets.json").read_text())
    assert immutable and "out/main.js" not in immutable
    assert all((web_workbench_assets / path).is_file() for path in immutable)
    workspace = Path(e2e_workspace_root_path)
    other = prepare_test_workspace(
        workspace_root=workspace / "B", template_root=context.project_root / "tests/fixtures/workspaces/default_test_workspace",
        template_items=(),
    )
    install_test_workspace_config(workspace_root=other, config_path=Path(e2e_workspace_config_path),
                                  schema_path=workspace / ".boxteam/workspace_schema.jsonc")
    for target, content in ((workspace, "原文件 A 🧪\n"), (other, "原文件 B 🧪\n")):
        (target / "cache-image.png").write_bytes(image_versions[0])
        (target / "cache-text.txt").write_text(content, encoding="utf-8")
    result_path = context.artifacts_dir / "file-cache.json"
    gateway = await asyncio.to_thread(start_gateway_process,
        workspace_root=workspace, default_backend_url=None, port=cache_gateway_port,
        boxteam_home=context.boxteam_home_for_node(request.node.nodeid), artifacts_dir=context.artifacts_dir / "gateway",
        extra_env={"BOXTEAM_WEB_ASSETS": str(web_workbench_assets), "LITELLM_LOCAL_MODEL_COST_MAP": "true"})
    command = ["bun", "tests/e2e/clients/web/quick_access_file_cache.mjs"]
    try:
        with chromium_temporary_environment(context.runtime_root_for_node(request.node.nodeid) / "chromium-tmp") as temporary:
            process = await asyncio.to_thread(subprocess.run, command, cwd=context.project_root,
                env={**os.environ, **temporary, "BOXTEAM_BROWSER_BASE_URL": f"http://127.0.0.1:{gateway.port}",
                     "BOXTEAM_BROWSER_RESULT_PATH": str(result_path), "BOXTEAM_CACHE_WORKSPACE": str(workspace),
                     "BOXTEAM_CACHE_OTHER": str(other), "BOXTEAM_CACHE_UPDATE": base64.b64encode(image_versions[1]).decode(),
                     "BOXTEAM_CACHE_ORIGINAL": base64.b64encode(image_versions[0]).decode(),
                     "PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH": chromium}, capture_output=True, text=True, timeout=150, check=False)
        (context.artifacts_dir / "browser.stdout.raw").write_text(process.stdout)
        (context.artifacts_dir / "browser.stderr.raw").write_text(process.stderr)
        (context.artifacts_dir / "browser-process.json").write_text(json.dumps({"command": command, "exit_code": process.returncode}) + "\n")
        assert process.returncode == 0, f"{process.stdout}\n{process.stderr}\n{result_path}"
        result = json.loads(result_path.read_text())
        assert result["phase"] == "passed" and result["pageErrors"] == []
        assert result["imageRevalidated"] and result["updatedImage"] and result["ownerIsolated"]
        assert result["watcherUpdatedImage"]
        assert result["deletedImageFailureVisible"] and result["deletedImageRecovery"]
        if os.name != "nt":
            assert result["permissionFailureVisible"] and result["permissionRecovery"]
        assert result["staticCachePolicy"]
        assert result["textRevalidated"] and result["staleSaveRejected"] and result["errorsNoStore"]
    finally:
        await asyncio.to_thread(close_gateway_process, gateway)
