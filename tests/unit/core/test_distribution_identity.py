"""`app/core/distribution_identity.py` 专属单测：编码单射性与 fail-closed。"""

from __future__ import annotations

import itertools
import json
from pathlib import Path

import pytest

from app.core import distribution_identity
from app.core.distribution_identity import (
    MANIFEST_ENV,
    encode_version,
    load_distribution_id,
)
from app.services.infrastructure.resource_platform.virtual_resources import (
    parse_vrn,
    skill_display_uri,
)


def _write_manifest(root: Path, payload: object) -> Path:
    manifest = root / "runtime-manifest.json"
    manifest.write_text(
        payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False),
        encoding="utf-8",
    )
    return manifest


def _valid_manifest(root: Path, **overrides: object) -> Path:
    payload: dict[str, object] = {
        "schema_version": 1,
        "distribution": "source-development",
        "version": "0.0.2",
    }
    payload.update(overrides)
    return _write_manifest(root, payload)


def test_encode_version_replaces_dots_with_underscores() -> None:
    assert encode_version("0.0.2") == "0_0_2"
    assert encode_version("1.0.0-beta.1") == "1_0_0-beta_1"
    assert encode_version("2.0") == "2_0"


def test_encode_version_is_injective_over_admitted_charset() -> None:
    """穷举模块自己承认的 version 字符集，证明编码单射（无碰撞）。

    样本字母表由 _VERSION_PATTERN 实际承认的字符导出——若有人放宽该 pattern 让 `_`
    重新合法（如回退到 `[A-Za-z0-9._-]`），字母表会包含 `_`，样本随之含连续下划线
    形态，编码立即出现碰撞，本用例变红。
    """
    admitted = [
        char
        for char in "ab019._-"
        if distribution_identity._VERSION_PATTERN.fullmatch(char) is not None
    ]
    # 下划线必须被排除，否则「`.`→`_`」与「原 `_`」同码、映射不再单射。
    assert "_" not in admitted
    assert set(admitted) == {"a", "b", "0", "1", "9", ".", "-"}
    samples = [
        "".join(chars)
        for size in (1, 2, 3, 4)
        for chars in itertools.product(admitted, repeat=size)
    ]
    assert len(samples) == len(admitted) + len(admitted) ** 2 + len(admitted) ** 3 + len(admitted) ** 4
    encoded = [encode_version(sample) for sample in samples]
    assert len(set(encoded)) == len(samples)
    # 单射即无碰撞：任一编码值只对应一个源样本，故可唯一还原。
    for sample, value in zip(samples, encoded):
        assert value.replace("_", ".") == sample


def test_manifest_rejects_underscore_in_version(tmp_path: Path) -> None:
    """version 字符集排除 `_`（semver 本不允许），含 `_` 一律 fail-closed。"""
    manifest = _valid_manifest(tmp_path, version="1_0")
    with pytest.raises(ValueError, match=r"只允许 \[A-Za-z0-9\.-\]"):
        load_distribution_id(manifest)


@pytest.mark.parametrize("field", ["distribution", "version"])
def test_missing_field_is_rejected(tmp_path: Path, field: str) -> None:
    payload = {
        "schema_version": 1,
        "distribution": "source-development",
        "version": "0.0.2",
    }
    payload.pop(field)
    manifest = _write_manifest(tmp_path, payload)
    with pytest.raises(ValueError, match=f"runtime manifest\\.{field} 必须是非空字符串"):
        load_distribution_id(manifest)


@pytest.mark.parametrize("field", ["distribution", "version"])
def test_blank_field_is_rejected(tmp_path: Path, field: str) -> None:
    manifest = _valid_manifest(tmp_path, **{field: "   "})
    with pytest.raises(ValueError, match=f"runtime manifest\\.{field} 必须是非空字符串"):
        load_distribution_id(manifest)


@pytest.mark.parametrize("bad", ["a/b", "a b", "a.b", "\u4e2d\u6587"])
def test_illegal_distribution_is_rejected(tmp_path: Path, bad: str) -> None:
    manifest = _valid_manifest(tmp_path, distribution=bad)
    with pytest.raises(
        ValueError, match="runtime manifest\\.distribution 含 VRN charset 之外的字符"
    ):
        load_distribution_id(manifest)


@pytest.mark.parametrize("bad", ["1 0", "1/0", "1.0.0+build", "v\u4e2d"])
def test_illegal_version_is_rejected(tmp_path: Path, bad: str) -> None:
    manifest = _valid_manifest(tmp_path, version=bad)
    with pytest.raises(ValueError, match="runtime manifest\\.version 只允许"):
        load_distribution_id(manifest)


def test_missing_manifest_file_is_rejected(tmp_path: Path) -> None:
    missing = tmp_path / "nope.json"
    with pytest.raises(FileNotFoundError, match="runtime manifest 不存在"):
        load_distribution_id(missing)


def test_manifest_path_is_directory_is_rejected(tmp_path: Path) -> None:
    directory = tmp_path / "manifest-dir"
    directory.mkdir()
    with pytest.raises(FileNotFoundError, match="runtime manifest 不存在"):
        load_distribution_id(directory)


def test_invalid_json_is_rejected(tmp_path: Path) -> None:
    manifest = _write_manifest(tmp_path, "{not json")
    with pytest.raises(ValueError, match="runtime manifest 不是合法 JSON"):
        load_distribution_id(manifest)


@pytest.mark.parametrize("payload", ["[]", "null", "\"text\"", "123"])
def test_non_object_json_is_rejected(tmp_path: Path, payload: str) -> None:
    manifest = _write_manifest(tmp_path, payload)
    with pytest.raises(ValueError, match="runtime manifest 必须是 JSON 对象"):
        load_distribution_id(manifest)


def test_missing_env_raises_and_never_falls_back_to_local(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """未设置 BOXTEAM_RUNTIME_MANIFEST 且未显式传参 → RuntimeError，不回退 local。"""
    monkeypatch.delenv(MANIFEST_ENV, raising=False)
    with pytest.raises(RuntimeError, match=MANIFEST_ENV):
        load_distribution_id()


def test_env_var_path_is_used(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manifest = _valid_manifest(tmp_path)
    monkeypatch.setenv(MANIFEST_ENV, str(manifest))
    assert load_distribution_id() == "source-development-0_0_2"


def test_legal_manifest_yields_grammar_valid_scope_id(tmp_path: Path) -> None:
    """输出必须能作为真实 VRN scope_id 被 parse_vrn 解析（接上真实 grammar）。"""
    manifest = _valid_manifest(tmp_path)
    distribution_id = load_distribution_id(manifest)
    assert distribution_id == "source-development-0_0_2"
    uri = skill_display_uri(
        scope="inline", scope_id=distribution_id, skill_name="gateway-context"
    )
    parsed = parse_vrn(uri)
    assert parsed.scope == "inline"
    assert parsed.scope_id == distribution_id
    assert parsed.kind == "skills"
    assert parsed.logical_name == "gateway-context"

