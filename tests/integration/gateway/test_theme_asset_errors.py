"""通过真实 Gateway 上传、重开、故障和删除验证主题图片 owner。"""

from __future__ import annotations

import json
from contextlib import ExitStack
from io import BytesIO
from pathlib import Path

import httpx
import pytest
from PIL import Image

from tests.harness.python.run_context import TestRunContext
from tests.support.ports import integration_port_block_for_file
from tests.support.processes.gateway import (
    LOCAL_TOKEN_HEADERS,
    close_gateway_process,
    reset_gateway_persistent_state,
    start_gateway_process,
)


@pytest.fixture
def theme_image_bytes() -> bytes:
    buffer = BytesIO()
    Image.new("RGB", (3, 2), "red").save(buffer, format="PNG")
    return buffer.getvalue()


@pytest.mark.asyncio
async def test_asset_manifest_errors_keep_identity_and_recover_through_gateway(
    request: pytest.FixtureRequest, integration_workspace_root_path: str, theme_image_bytes: bytes,
) -> None:
    context = TestRunContext.from_test_file(Path(__file__)).prepare()
    workspace = Path(integration_workspace_root_path)
    reset_gateway_persistent_state(workspace_root=workspace)
    ports = integration_port_block_for_file(Path(request.node.fspath))
    evidence = {}
    with ExitStack() as processes:
        gateway = start_gateway_process(workspace_root=workspace, default_backend_url=None, port=ports.port(0), artifacts_dir=context.artifacts_dir)
        processes.callback(close_gateway_process, gateway)
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{gateway.port}", headers=LOCAL_TOKEN_HEADERS, timeout=30) as client:
            created = await client.post("/api/gateway/users", json={"user_id": "theme-user", "display_name": "图片用户"})
            assert created.status_code == 200, created.text
            acquired = await client.post("/api/gateway/users/theme-user/access", json={"client_label": "theme-test"})
            assert acquired.status_code == 200, acquired.text
            upload = await client.post("/api/gateway/ui-assets", files={"file": ("background.png", theme_image_bytes, "image/png")})
            assert upload.status_code == 200, upload.text
            asset = upload.json()["data"]
            asset_id = asset["asset_id"]
            profile_root = context.boxteam_home / "state/gateway/users/theme-user"
            root = profile_root / "themes/assets/ui-assets"
            manifest = root / "manifest.json"
            image_path = root / asset_id / "original.png"
            original = manifest.read_bytes()
            assert image_path.read_bytes() == theme_image_bytes
            assert str(root) not in upload.text

            # 关键状态来自上传入口；外部损坏不得被重写、重建或转换成新的资源身份。
            unsafe = "/private/theme-secret.png"
            bad_filename = json.loads(original)
            bad_filename[asset_id]["original_filename"] = unsafe
            corruptions = [
                ("shape", b"[]"), ("json", b'{"native":"/private/theme-secret.png",}'),
                ("utf8", b'\xff/private/theme-secret.png'), ("filename", json.dumps(bad_filename).encode()),
            ]
            for field in ("content_type", "size", "imported_at", "referenced_theme_ids"):
                damaged = json.loads(original)
                damaged[asset_id][field] = [unsafe] if field == "referenced_theme_ids" else unsafe
                corruptions.append((field, json.dumps(damaged).encode()))
            for kind, content in corruptions:
                manifest.write_bytes(content)
                for method, endpoint, expected in [
                    ("GET", "/api/gateway/ui-assets", 409), ("GET", asset["url"], 409),
                    # 删除的最终存储阶段原先只捕获 KeyError，损坏清单沿用 500。
                    ("POST", "/api/gateway/ui-assets", 400), ("DELETE", asset["url"], 500),
                ]:
                    options = {"files": {"file": ("background.png", theme_image_bytes, "image/png")}} if method == "POST" else {}
                    response = await client.request(method, endpoint, **options)
                    assert response.status_code == expected, response.text
                    assert response.json()["request_id"] == response.headers["X-Request-ID"]
                    assert str(root) not in response.text and "theme-secret" not in response.text
                    assert manifest.read_bytes() == content and image_path.read_bytes() == theme_image_bytes
                    evidence[f"{kind}-{method}"] = response.json()
                manifest.write_bytes(original)
                reopened = await client.get(asset["url"])
                assert reopened.status_code == 200 and reopened.content == theme_image_bytes
                assert reopened.headers["etag"] == f'"{asset_id}"'
                duplicate = await client.post("/api/gateway/ui-assets", files={"file": ("renamed.png", theme_image_bytes, "image/png")})
                assert duplicate.status_code == 200 and duplicate.json()["data"] == asset
                assert manifest.read_bytes() == original

            profile = profile_root / "profile.jsonc"
            old_profile = profile.read_bytes()
            bad_background = await client.put("/api/gateway/ui-settings", json={"theme": {"background": {"type": "remote", "url": "file:///private/theme-secret.png"}}})
            assert bad_background.status_code == 409, bad_background.text
            assert "scheme=file" in bad_background.json()["detail"]
            assert "theme-secret" not in bad_background.text and profile.read_bytes() == old_profile
            evidence["file-url"] = bad_background.json()

            image_path.unlink()
            missing = await client.get(asset["url"])
            assert missing.status_code == 409 and str(root) not in missing.text
            assert manifest.read_bytes() == original
            image_path.write_bytes(theme_image_bytes)
            assert (await client.get(asset["url"])).content == theme_image_bytes
            deleted = await client.delete(asset["url"])
            assert deleted.status_code == 200 and deleted.json()["data"]["items"] == []
            assert not image_path.exists() and json.loads(manifest.read_bytes()) == {}
            evidence["deleted"] = deleted.json()
        logs = (context.artifacts_dir / "gateway.stderr.log").read_text()
        assert str(manifest) in logs and "file:///private/theme-secret.png" in logs
        (context.artifacts_dir / "theme-asset-errors.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n")
