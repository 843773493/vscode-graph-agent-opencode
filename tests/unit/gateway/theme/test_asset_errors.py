"""主题图片的原生错误与损坏清单不能变成普通位置引用。"""

from __future__ import annotations

import errno
import hashlib
import json
from functools import partial
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image

from app.gateway.theme import assets


@pytest.fixture
def image_bytes() -> tuple[bytes, bytes]:
    images = []
    for color in ("red", "blue"):
        buffer = BytesIO()
        Image.new("RGB", (2, 2), color).save(buffer, format="PNG")
        images.append(buffer.getvalue())
    return images[0], images[1]


@pytest.fixture
def image_store(tmp_path: Path, image_bytes):
    root = tmp_path / "private-theme-root"
    asset = assets.import_ui_asset(image_bytes[0], original_filename="first.png", gateway_root=root)
    path, _ = assets.resolve_ui_asset(asset.asset_id, gateway_root=root)
    return root, asset, path


@pytest.fixture(params=["/private/theme-secret", r"C:\private\theme-secret", r"\\server\private\theme-secret"])
def native_path(request: pytest.FixtureRequest) -> str:
    return request.param


@pytest.mark.parametrize("operation", ["manifest-read", "manifest-write", "image-write", "image-read", "image-delete"])
def test_asset_storage_error_keeps_raw_cause_and_recovery(
    image_store, image_bytes, native_path, operation, tmp_path, monkeypatch, asset_diagnostics,
) -> None:
    root, asset, path = image_store
    manifest = root / "ui-assets/manifest.json"
    original = manifest.read_bytes()
    source = tmp_path / "source.png"
    source.write_bytes(image_bytes[1])
    second_path = root / "ui-assets" / hashlib.sha256(image_bytes[1]).hexdigest() / "original.png"
    method, target, action = {
        "manifest-read": ("read_text", manifest, partial(assets.list_ui_assets, root)),
        "manifest-write": ("replace", manifest.with_suffix(".json.tmp"), partial(assets.import_ui_asset, image_bytes[1], original_filename="second.png", gateway_root=root)),
        "image-write": ("write_bytes", second_path, partial(assets.import_ui_asset, image_bytes[1], original_filename="second.png", gateway_root=root)),
        "image-read": ("open", path, partial(assets.resolve_ui_asset, asset.asset_id, gateway_root=root)),
        "image-delete": ("unlink", path, partial(assets.delete_ui_asset, asset.asset_id, gateway_root=root)),
    }[operation]
    raw_error = PermissionError(errno.EACCES, f"文件系统拒绝 {native_path}", native_path)
    real_method = getattr(Path, method)

    def fail(target_path: Path, *args: object, **kwargs: object):
        if target_path == target:
            raise raw_error
        return real_method(target_path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, method, fail)
        with pytest.raises(PermissionError) as caught:
            action()
    assert caught.value.__cause__ is raw_error
    assert caught.value.filename is None
    assert f"operation={operation}" in str(caught.value)
    assert "error_type=PermissionError" in str(caught.value) and f"errno={errno.EACCES}" in str(caught.value)
    assert native_path not in str(caught.value) and str(root) not in str(caught.value)
    records, sink_error = asset_diagnostics
    assert len(records) == 1 and records[0].exc_info[1] is raw_error
    assert native_path in str(raw_error)
    if sink_error is not None:
        assert "diagnostic_sink_error=PermissionError" in str(caught.value)
        assert str(sink_error) in "".join(raw_error.__notes__)
        assert sink_error.filename not in str(caught.value)
    assert manifest.read_bytes() == original and path.read_bytes() == image_bytes[0]
    action()
    if operation == "image-delete":
        assert not path.exists() and assets.list_ui_assets(root) == []
    else:
        resolved, same_asset = assets.resolve_ui_asset(asset.asset_id, gateway_root=root)
        assert resolved.read_bytes() == image_bytes[0] and same_asset.asset_id == asset.asset_id


@pytest.mark.parametrize("kind", ["shape", "json", "utf8", "key", "identity", "digest", "original-filename", "filename", "content-type", "size", "imported-at"])
def test_manifest_failure_never_rewrites_or_exposes_native_content(image_store, native_path, kind, asset_diagnostics) -> None:
    root, asset, _ = image_store
    path = root / "ui-assets/manifest.json"
    original = path.read_bytes()
    data = json.loads(original)
    if kind == "shape":
        content = b"[]"
    elif kind == "json":
        content = (json.dumps({"native": native_path}) + ",").encode()
    elif kind == "utf8":
        content = b"\xff" + native_path.encode()
    else:
        if kind == "key":
            data = {native_path: data[asset.asset_id]}
        else:
            field = {"identity": "asset_id", "digest": "sha256", "original-filename": "original_filename", "filename": "filename", "content-type": "content_type", "size": "size", "imported-at": "imported_at"}[kind]
            data[asset.asset_id][field] = native_path
        content = json.dumps(data).encode()
    path.write_bytes(content)
    with pytest.raises(ValueError) as caught:
        assets.list_ui_assets(root)
    assert native_path not in str(caught.value) and str(root) not in str(caught.value)
    records, sink_error = asset_diagnostics
    assert len(records) == 1 and records[0].exc_info[1] is caught.value.__cause__
    if sink_error is not None:
        assert "diagnostic_sink_error=PermissionError" in str(caught.value)
        assert str(sink_error) in "".join(caught.value.__cause__.__notes__)
        assert sink_error.filename not in str(caught.value)
    assert path.read_bytes() == content
    path.write_bytes(original)
    assert assets.list_ui_assets(root)[0].asset_id == asset.asset_id


def test_valid_metadata_preserves_integer_size(image_store, image_bytes) -> None:
    root, _, _ = image_store
    reopened, = assets.list_ui_assets(root)
    assert reopened.size == len(image_bytes[0]) and reopened.content_type == "image/png"


def test_path_like_asset_identity_is_rejected_without_echo(image_store, native_path) -> None:
    root, _, _ = image_store
    for action in (assets.resolve_ui_asset, assets.delete_ui_asset):
        with pytest.raises(KeyError, match="UI 资源不存在") as caught:
            action(native_path, gateway_root=root)
        assert native_path not in str(caught.value)
