"""图片编解码的统一有界入口；原件定位和模型能力判断仍归调用方。"""

from __future__ import annotations

from io import BytesIO
from math import sqrt
from typing import BinaryIO

from app.abstractions.resource_file_content import ResourceFileContent

IMAGE_SOURCE_MAX_BYTES = 30 * 1024 * 1024
IMAGE_SOURCE_MAX_PIXELS = 64_000_000
MODEL_IMAGE_MAX_EDGE = 2048
MODEL_IMAGE_MAX_PIXELS = 2_500_000
MODEL_IMAGE_MAX_BYTES = 4 * 1024 * 1024


def prepare_image(source: BinaryIO, *, max_edge: int = MODEL_IMAGE_MAX_EDGE) -> ResourceFileContent:
    """读取可定位的图片流，返回首帧 WebP 预览；不放大、不修改原件。

    调用方可为 UI 请求较小缩略图，模型入口统一使用默认尺寸。
    源字节与像素在解码前检查；编码只执行一次，不循环试探压缩质量。
    """
    from PIL import Image, ImageOps

    source.seek(0, 2)
    size = source.tell()
    source.seek(0)
    if size > IMAGE_SOURCE_MAX_BYTES:
        raise ValueError(f"图片源文件超过 {IMAGE_SOURCE_MAX_BYTES} 字节上限：{size}")
    try:
        image = Image.open(source)
    except Image.DecompressionBombError as error:
        raise ValueError(f"图片源像素超过 {IMAGE_SOURCE_MAX_PIXELS} 上限") from error
    with image:
        width, height = image.size
        pixels = width * height
        if pixels > IMAGE_SOURCE_MAX_PIXELS:
            raise ValueError(f"图片源像素超过 {IMAGE_SOURCE_MAX_PIXELS} 上限：{width}×{height}")
        scale = min(1.0, sqrt(MODEL_IMAGE_MAX_PIXELS / pixels))
        bounds = (min(max_edge, max(1, int(width * scale))), min(max_edge, max(1, int(height * scale))))
        image.thumbnail(bounds, Image.Resampling.LANCZOS)
        # 先缩小再纠正 EXIF，避免为原分辨率额外复制整张图片。
        with ImageOps.exif_transpose(image) as normalized:
            if normalized.mode not in {"RGB", "RGBA"}:
                mode = "RGBA" if "A" in normalized.getbands() or "transparency" in normalized.info else "RGB"
                converted = normalized.convert(mode)
            else:
                converted = normalized
            try:
                with BytesIO() as output:
                    converted.save(output, format="WEBP", quality=78, method=4)
                    data = output.getvalue()
            finally:
                if converted is not normalized:
                    converted.close()
    if len(data) > MODEL_IMAGE_MAX_BYTES:
        raise ValueError(f"图片预览超过 {MODEL_IMAGE_MAX_BYTES} 字节上限：{len(data)}")
    return ResourceFileContent(data, "image/webp")
