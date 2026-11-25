"""共享图片准备的解码边界、预览预算与图像语义。"""

from __future__ import annotations

import struct
import zlib
from io import BytesIO

import pytest
from PIL import Image

from app.runtime import image_preparation as preparation


@pytest.mark.parametrize("size", [(80, 60), (6000, 1000), (2400, 2400)])
def test_preview_respects_pixel_and_edge_budgets_without_enlarging(size):
    with BytesIO() as source, Image.new("RGB", size, "red") as image:
        image.save(source, format="PNG")
        original = source.getvalue()
        result = preparation.prepare_image(source)
        assert source.getvalue() == original
    assert result.content_type == "image/webp"
    assert len(result.data) <= preparation.MODEL_IMAGE_MAX_BYTES
    with Image.open(BytesIO(result.data)) as preview:
        assert preview.width <= size[0] and preview.height <= size[1]
        assert max(preview.size) <= preparation.MODEL_IMAGE_MAX_EDGE
        assert preview.width * preview.height <= preparation.MODEL_IMAGE_MAX_PIXELS
        if size == (80, 60):
            assert preview.size == size


@pytest.mark.parametrize("size", [(8001, 8000), (20000, 20000)])
def test_source_pixels_rejected_before_decoding(size, monkeypatch):
    with BytesIO() as source, Image.new("RGB", (1, 1)) as image:
        image.save(source, format="PNG")
        data = bytearray(source.getvalue())
    # 只改变 IHDR 和校验和；正文仍是 1×1，任何真正解码都会失败。
    data[16:24] = struct.pack(">II", *size)
    data[29:33] = struct.pack(">I", zlib.crc32(data[12:29]))

    def forbidden_decode(*args, **kwargs):
        pytest.fail("超限图片不能进入像素解码")

    monkeypatch.setattr(Image.Image, "thumbnail", forbidden_decode)
    with pytest.raises(ValueError, match="图片源像素超过 64000000"):
        preparation.prepare_image(BytesIO(data))


def test_source_bytes_rejected_before_open(tmp_path, monkeypatch):
    path = tmp_path / "oversized.png"
    with path.open("wb") as source:
        source.truncate(preparation.IMAGE_SOURCE_MAX_BYTES + 1)

    def forbidden_open(*args, **kwargs):
        pytest.fail("源字节超限不能进入图片解析")

    monkeypatch.setattr(Image, "open", forbidden_open)
    with path.open("rb") as source, pytest.raises(ValueError, match="图片源文件超过"):
        preparation.prepare_image(source)


def test_preview_bytes_limit_is_enforced(monkeypatch):
    monkeypatch.setattr(preparation, "MODEL_IMAGE_MAX_BYTES", 1)
    with BytesIO() as source, Image.new("RGB", (32, 32)) as image:
        image.save(source, format="PNG")
        with pytest.raises(ValueError, match="图片预览超过"):
            preparation.prepare_image(source)


def test_preview_preserves_transparency_and_applies_exif():
    with BytesIO() as source, Image.new("RGBA", (80, 40), (200, 0, 0, 0)) as image:
        image.save(source, format="PNG")
        result = preparation.prepare_image(source)
    with Image.open(BytesIO(result.data)) as preview:
        assert preview.size == (80, 40) and preview.getpixel((0, 0))[3] == 0
    with BytesIO() as source, Image.new("RGB", (400, 200), "red") as image:
        exif = Image.Exif()
        exif[274] = 6
        image.save(source, format="JPEG", exif=exif)
        result = preparation.prepare_image(source)
    with Image.open(BytesIO(result.data)) as preview:
        assert preview.size == (200, 400)
