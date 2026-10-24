from __future__ import annotations

import hashlib
import io
import json
import logging
import re
from datetime import UTC, datetime
from pathlib import Path, PureWindowsPath
from typing import TypedDict, cast

from PIL import Image

from app.schemas.gateway.ui_settings import GatewayUIAssetDTO

logger = logging.getLogger(__name__)
_ASSET_ID = re.compile(r"[a-f0-9]{64}")
_THEME_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")
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


class _AssetRecord(TypedDict):
    asset_id: str
    original_filename: str
    content_type: str
    size: int
    sha256: str
    imported_at: str
    filename: str
    referenced_theme_ids: list[str]


def ui_assets_root(gateway_root: Path) -> Path:
    return gateway_root / "ui-assets"


def _manifest_path(gateway_root: Path) -> Path:
    return ui_assets_root(gateway_root) / "manifest.json"


def _read_manifest(gateway_root: Path) -> dict[str, _AssetRecord]:
    manifest_path = _manifest_path(gateway_root)
    try:
        manifest_text = manifest_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError as error:
        raise _asset_io_error("manifest-read", error) from error
    except UnicodeDecodeError as error:
        logger.exception("Gateway UI 资源清单编码无效")
        raise ValueError(
            f"Gateway UI 资源清单编码无效: encoding={error.encoding}, start={error.start}, end={error.end}"
        ) from error
    try:
        manifest_data = json.loads(manifest_text)
    except json.JSONDecodeError as error:
        logger.exception("Gateway UI 资源清单 JSON 无效")
        raise ValueError(
            f"Gateway UI 资源清单 JSON 无效: line={error.lineno}, column={error.colno}"
        ) from error
    if not isinstance(manifest_data, dict) or not all(
        isinstance(key, str) and isinstance(value, dict)
        for key, value in manifest_data.items()
    ):
        logger.error("Gateway UI 资源清单格式无效: path=%s", manifest_path)
        raise ValueError("Gateway UI 资源清单格式无效")
    for asset_id, record in manifest_data.items():
        if (not _ASSET_ID.fullmatch(asset_id) or record.get("asset_id") != asset_id
                or record.get("sha256") != asset_id):
            raise ValueError("Gateway UI 资源清单身份与摘要不一致")
        original_filename = record.get("original_filename")
        filename = record.get("filename")
        if (not isinstance(original_filename, str) or not original_filename
                or PureWindowsPath(original_filename).name != original_filename
                or not isinstance(filename, str)
                or filename not in {"original.png", "original.jpg", "original.webp", "original.gif"}):
            raise ValueError(f"Gateway UI 资源清单文件名无效: asset_id={asset_id}")
        content_type = record.get("content_type")
        size = record.get("size")
        imported_at = record.get("imported_at")
        references = record.get("referenced_theme_ids")
        if (not isinstance(content_type, str) or content_type not in {mime for mime, _ in SUPPORTED_FORMATS.values()}
                or type(size) is not int or size < 0
                or not isinstance(imported_at, str)
                or not isinstance(references, list)
                or not all(isinstance(theme_id, str) and (theme_id == "当前 UI 设置" or _THEME_ID.fullmatch(theme_id)) for theme_id in references)):
            logger.error("Gateway UI 资源清单元数据无效: asset_id=%s, record=%r", asset_id, record)
            raise ValueError(f"Gateway UI 资源清单元数据无效: asset_id={asset_id}")
        try:
            timestamp = datetime.fromisoformat(imported_at)
        except ValueError as error:
            logger.exception("Gateway UI 资源清单时间无效: asset_id=%s", asset_id)
            raise ValueError(f"Gateway UI 资源清单时间无效: asset_id={asset_id}") from error
        if timestamp.tzinfo is None:
            raise ValueError(f"Gateway UI 资源清单时间缺少时区: asset_id={asset_id}")
    return cast(dict[str, _AssetRecord], manifest_data)


def _write_manifest(gateway_root: Path, manifest: dict[str, _AssetRecord]) -> None:
    manifest_path = _manifest_path(gateway_root)
    temporary_manifest_path = manifest_path.with_suffix(".json.tmp")
    try:
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        temporary_manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary_manifest_path.replace(manifest_path)
    except OSError as error:
        raise _asset_io_error("manifest-write", error) from error


def _validate_image(content: bytes) -> tuple[str, str, str]:
    if not content:
        raise ValueError("背景图片内容为空")
    _validate_image_size(len(content))
    with Image.open(io.BytesIO(content)) as image:
        image.verify()
        image_format = image.format
    if image_format not in SUPPORTED_FORMATS:
        raise ValueError("背景图片仅支持 PNG、JPEG、WebP 和 GIF")
    content_type, extension = SUPPORTED_FORMATS[image_format]
    return image_format, content_type, extension


def _validate_image_size(size: int) -> None:
    if size > MAX_UI_ASSET_BYTES:
        raise ValueError(f"背景图片超过 {MAX_UI_ASSET_BYTES // 1024 // 1024} MiB 限制")


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
    supplied_filename = PureWindowsPath(original_filename)
    original_extension = supplied_filename.suffix.lower()
    if original_extension not in SUPPORTED_EXTENSIONS[image_format]:
        allowed = ", ".join(sorted(SUPPORTED_EXTENSIONS[image_format]))
        raise ValueError(
            f"背景图片扩展名与内容格式不匹配: {original_extension or '<无扩展名>'}，"
            f"{image_format} 允许 {allowed}"
        )
    digest = hashlib.sha256(content).hexdigest()
    manifest = _read_manifest(gateway_root)
    asset_record = manifest.get(digest)
    if asset_record is None:
        asset_dir = ui_assets_root(gateway_root) / digest
        filename = f"original{extension}"
        asset_path = asset_dir / filename
        try:
            asset_dir.mkdir(parents=True, exist_ok=True)
            asset_path.write_bytes(content)
        except OSError as error:
            raise _asset_io_error("image-write", error) from error
        asset_record = {
            "asset_id": digest,
            "original_filename": supplied_filename.name or filename,
            "content_type": content_type,
            "size": len(content),
            "sha256": digest,
            "imported_at": datetime.now(UTC).isoformat(),
            "filename": filename,
            "referenced_theme_ids": [],
        }
        manifest[digest] = asset_record
        _write_manifest(gateway_root, manifest)
    return _asset_dto(asset_record)


def import_ui_asset_file(path: Path, *, gateway_root: Path) -> GatewayUIAssetDTO:
    source = path.expanduser()
    try:
        if source.is_symlink():
            logger.error("主题本地背景图片不允许使用符号链接: path=%s", source)
            raise ValueError("主题本地背景图片不允许使用符号链接")
        resolved = source.resolve()
        if not resolved.is_file():
            raise FileNotFoundError(f"主题本地背景图片不存在: {resolved}")
        _validate_image_size(resolved.stat().st_size)
        content = resolved.read_bytes()
    except OSError as error:
        raise _asset_io_error("local-image-read", error) from error
    return import_ui_asset(
        content,
        original_filename=resolved.name,
        gateway_root=gateway_root,
    )


def _asset_dto(asset_record: _AssetRecord) -> GatewayUIAssetDTO:
    asset_id = asset_record["asset_id"]
    return GatewayUIAssetDTO(
        asset_id=asset_id,
        original_filename=asset_record["original_filename"],
        content_type=asset_record["content_type"],
        size=asset_record["size"],
        sha256=asset_record["sha256"],
        imported_at=asset_record["imported_at"],
        url=f"/api/gateway/ui-assets/{asset_id}",
        referenced_theme_ids=sorted(asset_record["referenced_theme_ids"]),
    )


def list_ui_assets(gateway_root: Path) -> list[GatewayUIAssetDTO]:
    return sorted(
        (
            _asset_dto(asset_record)
            for asset_record in _read_manifest(gateway_root).values()
        ),
        key=lambda asset: asset.imported_at,
        reverse=True,
    )


def resolve_ui_asset(
    asset_id: str, *, gateway_root: Path
) -> tuple[Path, GatewayUIAssetDTO]:
    _require_asset_id(asset_id)
    asset_record = _read_manifest(gateway_root).get(asset_id)
    if asset_record is None:
        raise KeyError(f"Gateway UI 资源不存在: {asset_id}")
    filename = asset_record["filename"]
    try:
        asset_root = (ui_assets_root(gateway_root) / asset_id).resolve()
        asset_path = (asset_root / filename).resolve()
        if asset_path.parent != asset_root or not asset_path.is_file():
            raise FileNotFoundError(f"Gateway UI 资源清单与文件不一致: {asset_id}")
        with asset_path.open("rb") as asset_file:
            actual_digest = hashlib.file_digest(asset_file, "sha256").hexdigest()
    except OSError as error:
        raise _asset_io_error("image-read", error, asset_id=asset_id) from error
    if actual_digest != asset_id:
        raise ValueError(f"Gateway UI 资源摘要不匹配: {asset_id}")
    return asset_path, _asset_dto(asset_record)


def delete_ui_asset(asset_id: str, *, gateway_root: Path) -> None:
    _require_asset_id(asset_id)
    manifest = _read_manifest(gateway_root)
    asset_record = manifest.get(asset_id)
    if asset_record is None:
        raise KeyError(f"Gateway UI 资源不存在: {asset_id}")
    asset_path, _ = resolve_ui_asset(asset_id, gateway_root=gateway_root)
    try:
        asset_path.unlink()
        asset_path.parent.rmdir()
    except OSError as error:
        raise _asset_io_error("image-delete", error, asset_id=asset_id) from error
    del manifest[asset_id]
    _write_manifest(gateway_root, manifest)


def update_ui_asset_references(
    references: dict[str, list[str]],
    *,
    gateway_root: Path,
) -> None:
    manifest = _read_manifest(gateway_root)
    changed = False
    for asset_id, asset_record in manifest.items():
        referenced_theme_ids = sorted(set(references.get(asset_id, [])))
        if asset_record.get("referenced_theme_ids") == referenced_theme_ids:
            continue
        asset_record["referenced_theme_ids"] = referenced_theme_ids
        changed = True
    if changed:
        _write_manifest(gateway_root, manifest)


def _require_asset_id(asset_id: str) -> None:
    if not _ASSET_ID.fullmatch(asset_id):
        raise KeyError("Gateway UI 资源不存在: 无效资源标识")


def _asset_io_error(operation: str, error: OSError, *, asset_id: str | None = None) -> OSError:
    """保留原生故障诊断，只公开已核验的资源身份与操作。"""
    message = (
        f"Gateway UI 资源存储失败: operation={operation}, asset_id={asset_id}, "
        f"error_type={type(error).__name__}, errno={error.errno}"
    )
    logger.exception(message)
    return type(error)(message)
