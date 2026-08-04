from __future__ import annotations

import hashlib
import io
import json
from datetime import UTC, datetime
from pathlib import Path

from PIL import Image

from app.gateway.schemas import GatewayUIAssetDTO

MAX_UI_ASSET_BYTES = 20 * 1024 * 1024
SUPPORTED_FORMATS = {
    "PNG": ("image/png", ".png"),
    "JPEG": ("image/jpeg", ".jpg"),
    "WEBP": ("image/webp", ".webp"),
    "GIF": ("image/gif", ".gif"),
}
SUPPORTED_EXTENSIONS = {
    "PNG": {".png"},
    "JPEG": {".jpg", ".jpeg"},
    "WEBP": {".webp"},
    "GIF": {".gif"},
}


def ui_assets_root(gateway_root: Path) -> Path:
    return gateway_root / "ui-assets"


def _manifest_path(gateway_root: Path) -> Path:
    return ui_assets_root(gateway_root) / "manifest.json"


def _read_manifest(gateway_root: Path) -> dict[str, dict[str, object]]:
    path = _manifest_path(gateway_root)
    if not path.exists():
        return {}
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or not all(
        isinstance(key, str) and isinstance(value, dict) for key, value in raw.items()
    ):
        raise ValueError(f"Gateway UI 资源清单格式无效: {path}")
    return raw


def _write_manifest(gateway_root: Path, manifest: dict[str, dict[str, object]]) -> None:
    path = _manifest_path(gateway_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _validate_image(content: bytes) -> tuple[str, str, str]:
    if not content:
        raise ValueError("背景图片内容为空")
    if len(content) > MAX_UI_ASSET_BYTES:
        raise ValueError(f"背景图片超过 {MAX_UI_ASSET_BYTES // 1024 // 1024} MiB 限制")
    with Image.open(io.BytesIO(content)) as image:
        image.verify()
        image_format = image.format
    if image_format not in SUPPORTED_FORMATS:
        raise ValueError("背景图片仅支持 PNG、JPEG、WebP 和 GIF")
    content_type, extension = SUPPORTED_FORMATS[image_format]
    return image_format, content_type, extension


def import_ui_asset(
    content: bytes,
    *,
    original_filename: str,
    gateway_root: Path,
    declared_content_type: str | None = None,
) -> GatewayUIAssetDTO:
    image_format, content_type, extension = _validate_image(content)
    if declared_content_type is not None and declared_content_type != content_type:
        raise ValueError(
            "背景图片声明的 MIME type 与实际内容不匹配: "
            f"declared={declared_content_type} actual={content_type}"
        )
    original_extension = Path(original_filename).suffix.lower()
    if original_extension not in SUPPORTED_EXTENSIONS[image_format]:
        allowed = ", ".join(sorted(SUPPORTED_EXTENSIONS[image_format]))
        raise ValueError(
            f"背景图片扩展名与内容格式不匹配: {original_extension or '<无扩展名>'}，"
            f"{image_format} 允许 {allowed}"
        )
    digest = hashlib.sha256(content).hexdigest()
    manifest = _read_manifest(gateway_root)
    existing = manifest.get(digest)
    if existing is None:
        asset_dir = ui_assets_root(gateway_root) / digest
        asset_dir.mkdir(parents=True, exist_ok=True)
        filename = f"original{extension}"
        asset_path = asset_dir / filename
        asset_path.write_bytes(content)
        existing = {
            "asset_id": digest,
            "original_filename": Path(original_filename).name or filename,
            "content_type": content_type,
            "size": len(content),
            "sha256": digest,
            "imported_at": datetime.now(UTC).isoformat(),
            "filename": filename,
            "referenced_theme_ids": [],
        }
        manifest[digest] = existing
        _write_manifest(gateway_root, manifest)
    return _asset_dto(existing)


def import_ui_asset_file(path: Path, *, gateway_root: Path) -> GatewayUIAssetDTO:
    source = path.expanduser()
    if source.is_symlink():
        raise ValueError(f"主题本地背景图片不允许使用符号链接: {source}")
    resolved = source.resolve()
    if not resolved.is_file():
        raise FileNotFoundError(f"主题本地背景图片不存在: {resolved}")
    return import_ui_asset(
        resolved.read_bytes(),
        original_filename=resolved.name,
        gateway_root=gateway_root,
    )


def _asset_dto(entry: dict[str, object]) -> GatewayUIAssetDTO:
    asset_id = str(entry["asset_id"])
    return GatewayUIAssetDTO(
        asset_id=asset_id,
        original_filename=str(entry["original_filename"]),
        content_type=str(entry["content_type"]),
        size=int(str(entry["size"])),
        sha256=str(entry["sha256"]),
        imported_at=str(entry["imported_at"]),
        url=f"/api/gateway/ui-assets/{asset_id}",
        referenced_theme_ids=sorted(
            str(theme_id) for theme_id in entry.get("referenced_theme_ids", [])
        ),
    )


def list_ui_assets(gateway_root: Path) -> list[GatewayUIAssetDTO]:
    return sorted(
        (_asset_dto(entry) for entry in _read_manifest(gateway_root).values()),
        key=lambda item: item.imported_at,
        reverse=True,
    )


def resolve_ui_asset(
    asset_id: str, *, gateway_root: Path
) -> tuple[Path, GatewayUIAssetDTO]:
    entry = _read_manifest(gateway_root).get(asset_id)
    if entry is None:
        raise KeyError(f"Gateway UI 资源不存在: {asset_id}")
    filename = str(entry["filename"])
    asset_root = (ui_assets_root(gateway_root) / asset_id).resolve()
    asset_path = (asset_root / filename).resolve()
    if asset_path.parent != asset_root or not asset_path.is_file():
        raise FileNotFoundError(f"Gateway UI 资源清单与文件不一致: {asset_id}")
    content = asset_path.read_bytes()
    actual_digest = hashlib.sha256(content).hexdigest()
    if actual_digest != asset_id:
        raise ValueError(f"Gateway UI 资源摘要不匹配: {asset_id}")
    return asset_path, _asset_dto(entry)


def delete_ui_asset(asset_id: str, *, gateway_root: Path) -> None:
    manifest = _read_manifest(gateway_root)
    entry = manifest.get(asset_id)
    if entry is None:
        raise KeyError(f"Gateway UI 资源不存在: {asset_id}")
    path, _ = resolve_ui_asset(asset_id, gateway_root=gateway_root)
    path.unlink()
    path.parent.rmdir()
    del manifest[asset_id]
    _write_manifest(gateway_root, manifest)


def update_ui_asset_references(
    references: dict[str, list[str]],
    *,
    gateway_root: Path,
) -> None:
    manifest = _read_manifest(gateway_root)
    changed = False
    for asset_id, entry in manifest.items():
        referenced_theme_ids = sorted(set(references.get(asset_id, [])))
        if entry.get("referenced_theme_ids") == referenced_theme_ids:
            continue
        entry["referenced_theme_ids"] = referenced_theme_ids
        changed = True
    if changed:
        _write_manifest(gateway_root, manifest)
