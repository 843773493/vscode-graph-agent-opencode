"""发行身份拒绝只公布逻辑阶段，安装位置与非法原值保留诊断。"""

from __future__ import annotations

import errno
import json
from pathlib import Path

import pytest

from app.core.distribution_identity import load_distribution_id


@pytest.fixture
def manifest_case(tmp_path):
    path = tmp_path / "private-installation" / "runtime-manifest.json"
    path.parent.mkdir()
    original = json.dumps({"distribution": "standalone", "version": "1.2.3"}).encode()
    path.write_bytes(original)
    return path, original


@pytest.mark.parametrize("failure", ["missing", "directory", "json", "object", "distribution", "version"])
def test_manifest_rejection_keeps_original_input_and_recovers_after_repair(manifest_case, caplog, failure):
    path, original = manifest_case
    private_value = "/manifest-private/secret-version"
    if failure == "missing":
        path.unlink()
    elif failure == "directory":
        path.unlink()
        path.mkdir()
    elif failure == "json":
        path.write_text('{"manifest-private":', encoding="utf-8")
    elif failure == "object":
        path.write_text('["manifest-private"]', encoding="utf-8")
    else:
        payload = json.loads(original)
        payload[failure] = private_value
        path.write_text(json.dumps(payload), encoding="utf-8")
    before = path.read_bytes() if path.is_file() else None
    expected = FileNotFoundError if failure in {"missing", "directory"} else ValueError
    with pytest.raises(expected) as caught:
        load_distribution_id(path)
    assert str(path) not in str(caught.value)
    assert "manifest-private" not in str(caught.value)
    assert "artifact=runtime_manifest" in str(caught.value)
    assert "stage=" in str(caught.value)
    assert str(path) in caplog.text
    if failure in {"distribution", "version"}:
        assert private_value in caplog.text and failure in str(caught.value)
    if failure == "json":
        assert isinstance(caught.value.__cause__, json.JSONDecodeError)
    if before is not None:
        assert path.read_bytes() == before
    if path.is_dir():
        path.rmdir()
    path.write_bytes(original)
    assert load_distribution_id(path) == "standalone-1_2_3"
    assert path.read_bytes() == original


@pytest.mark.parametrize("native", [r"C:\manifest-private\version", r"\\manifest-private\share\version"])
@pytest.mark.parametrize("field", ["distribution", "version"])
def test_rejected_field_never_copies_native_values_into_public_error(manifest_case, caplog, field, native):
    path, original = manifest_case
    payload = json.loads(original)
    payload[field] = native
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError) as caught:
        load_distribution_id(path)
    assert "manifest-private" not in str(caught.value)
    assert repr(native) in caplog.text
    assert field in str(caught.value)


def test_manifest_read_failure_retains_native_type_codes_and_direct_cause(manifest_case, monkeypatch, caplog):
    path, original = manifest_case
    read_text = Path.read_text
    failure = PermissionError(errno.EACCES, "private native message", str(path))

    def denied(candidate, *args, **kwargs):
        if candidate == path:
            raise failure
        return read_text(candidate, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "read_text", denied)
        with pytest.raises(PermissionError) as caught:
            load_distribution_id(path)
    assert caught.value.__cause__ is failure
    assert caught.value.errno == failure.errno
    assert getattr(caught.value, "winerror", None) == getattr(failure, "winerror", None)
    assert caught.value.filename is None
    assert str(path) not in str(caught.value) and "private native message" not in str(caught.value)
    assert "stage=read" in str(caught.value) and str(path) in caplog.text
    assert path.read_bytes() == original
    assert load_distribution_id(path) == "standalone-1_2_3"


def test_manifest_resolution_failure_retains_runtime_cause_without_copying_position(manifest_case, monkeypatch, caplog):
    path, original = manifest_case
    resolve = Path.resolve
    failure = RuntimeError(f"private resolver position: {path}")

    def denied(candidate, *args, **kwargs):
        if candidate == path:
            raise failure
        return resolve(candidate, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "resolve", denied)
        with pytest.raises(RuntimeError) as caught:
            load_distribution_id(path)
    assert caught.value.__cause__ is failure
    assert str(path) not in str(caught.value)
    assert "stage=resolve" in str(caught.value) and str(path) in caplog.text
    assert path.read_bytes() == original
    assert load_distribution_id(path) == "standalone-1_2_3"


def test_manifest_invalid_utf8_keeps_exact_decode_facts_and_original_bytes(manifest_case, caplog):
    path, original = manifest_case
    damaged = b'{"version":"private-version\xff"}'
    path.write_bytes(damaged)
    with pytest.raises(UnicodeDecodeError) as caught:
        load_distribution_id(path)
    source = caught.value.__cause__
    assert isinstance(source, UnicodeDecodeError)
    assert (caught.value.encoding, caught.value.object, caught.value.start, caught.value.end) == (
        source.encoding, source.object, source.start, source.end,
    )
    assert "artifact=runtime_manifest" in str(caught.value) and "stage=decode" in str(caught.value)
    assert str(path) not in str(caught.value) and str(path) in caplog.text
    assert path.read_bytes() == damaged
    path.write_bytes(original)
    assert load_distribution_id(path) == "standalone-1_2_3"
