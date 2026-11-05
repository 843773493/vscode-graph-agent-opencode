"""正文成功校验的作用域、完整输入身份与有界内存合同。"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import nullcontext

import pytest

from app.domain.itemized.hashing import payload_content_length
from app.domain.itemized.records import CanonicalItemRecord
from app.services.infrastructure.rollout_context.storage.catalog import integrity
from app.services.infrastructure.rollout_context.storage.serialization import (
    canonical_json_line,
)

_BodyInputFactory = Callable[..., tuple[bytes, dict[str, object]]]


@pytest.fixture
def body_input_factory() -> _BodyInputFactory:
    def create(
        item_id: str = "body-validation", payload: str = "original",
    ) -> tuple[bytes, dict[str, object]]:
        item = CanonicalItemRecord.create(
            item_sequence=1, item_id=item_id, semantic_kind="user_input",
            payload_kind="text", status="completed", payload=payload,
            producer_ref={"producer_kind": "user", "producer_id": "body-ingress"},
            created_at="2026-10-17T00:00:00+00:00",
        )
        return canonical_json_line(item.to_dict()), {
            "sequence": item.item_sequence, "item_id": item.item_id,
            "catalog_hash": item.content_hash, "payload_length": len(payload.encode("utf-8")),
            "source_revision": f"canonical:{item.item_id}:{item.content_hash}",
        }

    return create


@pytest.fixture
def validation_calls(monkeypatch: pytest.MonkeyPatch) -> list[object]:
    calls: list[object] = []
    original = integrity.canonical_json_line

    def observed(value: object) -> bytes:
        calls.append(value)
        return original(value)

    monkeypatch.setattr(integrity, "canonical_json_line", observed)
    return calls


def test_body_validation_reuses_success_only_inside_explicit_scope(
    body_input_factory: _BodyInputFactory, validation_calls: list[object],
) -> None:
    raw, parameters = body_input_factory()
    with integrity.catalog_body_validation_scope():
        integrity.validate_catalog_body(raw, **parameters)
        integrity.validate_catalog_body(raw, **parameters)
        assert len(validation_calls) == 1
    integrity.validate_catalog_body(raw, **parameters)
    integrity.validate_catalog_body(raw, **parameters)
    assert len(validation_calls) == 3
    with integrity.catalog_body_validation_scope():
        integrity.validate_catalog_body(raw, **parameters)
    assert len(validation_calls) == 4


def test_body_validation_resets_scope_after_exception_and_restores_nested_scope(
    body_input_factory: _BodyInputFactory, validation_calls: list[object],
) -> None:
    raw, parameters = body_input_factory()
    with pytest.raises(RuntimeError, match="scope failure"), integrity.catalog_body_validation_scope():
        integrity.validate_catalog_body(raw, **parameters)
        with integrity.catalog_body_validation_scope():
            integrity.validate_catalog_body(raw, **parameters)
        integrity.validate_catalog_body(raw, **parameters)
        assert len(validation_calls) == 2
        raise RuntimeError("scope failure")
    integrity.validate_catalog_body(raw, **parameters)
    assert len(validation_calls) == 3


@pytest.mark.parametrize("fail", [False, True])
def test_borrowed_scope_keeps_outer_success_until_owner_exits(
    body_input_factory: _BodyInputFactory, validation_calls: list[object], fail: bool,
) -> None:
    raw, parameters = body_input_factory()
    with integrity.catalog_body_validation_scope():
        integrity.validate_catalog_body(raw, **parameters)
        try:
            with integrity.catalog_body_validation_scope(reuse_existing=True):
                integrity.validate_catalog_body(raw, **parameters)
                if fail:
                    raise RuntimeError("borrowed scope failure")
        except RuntimeError as error:
            assert fail and str(error) == "borrowed scope failure"
        integrity.validate_catalog_body(raw, **parameters)
        assert len(validation_calls) == 1
    integrity.validate_catalog_body(raw, **parameters)
    assert len(validation_calls) == 2


def test_borrowed_scope_respects_outer_capacity(
    body_input_factory: _BodyInputFactory, validation_calls: list[object],
) -> None:
    first, first_parameters = body_input_factory("first")
    second, second_parameters = body_input_factory("other")
    with integrity.catalog_body_validation_scope(max_entries=1):
        integrity.validate_catalog_body(first, **first_parameters)
        with integrity.catalog_body_validation_scope(reuse_existing=True):
            integrity.validate_catalog_body(second, **second_parameters)
            integrity.validate_catalog_body(first, **first_parameters)
    assert len(validation_calls) == 3


@pytest.mark.parametrize("scoped", [False, True])
def test_body_validation_returns_isolated_nested_values_on_every_read(
    validation_calls: list[object], scoped: bool,
) -> None:
    item = CanonicalItemRecord.create(
        item_sequence=1, item_id="nested-validation", semantic_kind="user_input",
        payload_kind="structured_content", status="completed", payload={"text": ["original"]},
        producer_ref={"producer_kind": "user", "producer_id": "body-ingress"},
        created_at="2026-10-19T00:00:00+00:00",
    )
    raw = canonical_json_line(item.to_dict())
    parameters = {
        "sequence": item.item_sequence, "item_id": item.item_id,
        "catalog_hash": item.content_hash,
        "payload_length": payload_content_length(item.payload_kind, item.payload),
        "source_revision": f"canonical:{item.item_id}:{item.content_hash}",
    }
    with integrity.catalog_body_validation_scope() if scoped else nullcontext():
        first = integrity.read_catalog_body(raw, **parameters)
        assert isinstance(first.payload, dict)
        assert isinstance(first.payload["text"], list)
        assert isinstance(first.producer_ref, dict)
        first.payload["text"].append("caller mutation")
        first.producer_ref["producer_id"] = "caller mutation"
        second = integrity.read_catalog_body(raw, **parameters)
        assert second.to_dict() == item.to_dict()
        assert isinstance(second.payload, dict)
        assert isinstance(second.payload["text"], list)
        second.payload["text"].append("second caller mutation")
        third = integrity.read_catalog_body(raw, **parameters)
        assert third.to_dict() == item.to_dict()
    assert len(validation_calls) == (1 if scoped else 3)
    assert integrity.read_catalog_body(raw, **parameters).to_dict() == item.to_dict()
    assert len(validation_calls) == (2 if scoped else 4)


def test_unscoped_body_read_does_not_copy_a_newly_parsed_item(
    body_input_factory: _BodyInputFactory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw, parameters = body_input_factory()

    def unexpected_copy(_item):
        raise AssertionError("无缓存作用域的解析结果已独占，不需要再次深拷贝")

    monkeypatch.setattr(integrity, "deepcopy", unexpected_copy)
    first = integrity.read_catalog_body(raw, **parameters)
    first.producer_ref["producer_id"] = "caller mutation"
    second = integrity.read_catalog_body(raw, **parameters)
    assert second.producer_ref["producer_id"] == "body-ingress"


@pytest.mark.parametrize(("field", "changed"), [
    ("sequence", 2), ("item_id", "another-id"), ("catalog_hash", "another-hash"),
    ("payload_length", 100), ("source_revision", "another-revision"),
])
@pytest.mark.parametrize("retained", [False, True])
def test_body_validation_binds_every_catalog_parameter(
    body_input_factory: _BodyInputFactory, validation_calls: list[object], field: str, changed: object,
    retained: bool,
) -> None:
    raw, parameters = body_input_factory()
    cache = integrity.CatalogBodyValidationCache() if retained else None
    with integrity.catalog_body_validation_scope():
        integrity.validate_catalog_body(raw, **parameters, cache=cache)
        with pytest.raises(RuntimeError, match="不一致"):
            integrity.validate_catalog_body(raw, **{**parameters, field: changed}, cache=cache)
        integrity.validate_catalog_body(raw, **parameters, cache=cache)
    assert len(validation_calls) == 2


@pytest.mark.parametrize("retained", [False, True])
def test_body_validation_does_not_reuse_same_length_changed_bytes_or_cache_failures(
    body_input_factory: _BodyInputFactory, validation_calls: list[object], retained: bool,
) -> None:
    raw, parameters = body_input_factory()
    damaged = raw.replace(b'"payload":"original"', b'"payload":"modified"')
    assert damaged != raw and len(damaged) == len(raw)
    cache = integrity.CatalogBodyValidationCache() if retained else None
    with integrity.catalog_body_validation_scope():
        integrity.validate_catalog_body(raw, **parameters, cache=cache)
        for _ in range(2):
            with pytest.raises(RuntimeError, match="schema/content 非法"):
                integrity.validate_catalog_body(damaged, **parameters, cache=cache)
        integrity.validate_catalog_body(raw, **parameters, cache=cache)
    assert len(validation_calls) == 3


def test_retained_receipts_reuse_success_across_scopes_without_retaining_items(
    body_input_factory: _BodyInputFactory, validation_calls: list[object],
) -> None:
    raw, parameters = body_input_factory()
    cache = integrity.CatalogBodyValidationCache()
    for _ in range(2):
        with integrity.catalog_body_validation_scope():
            integrity.validate_catalog_body(raw, **parameters, cache=cache)
    assert len(validation_calls) == 1
    # 回执只服务校验，不能把先前调用的可变 item 或正文当作新的读取结果。
    integrity.read_catalog_body(raw, **parameters)
    assert len(validation_calls) == 2
    integrity.validate_catalog_body(raw, **parameters, cache=integrity.CatalogBodyValidationCache())
    assert len(validation_calls) == 3


def test_retained_receipts_evict_old_success_at_capacity(
    body_input_factory: _BodyInputFactory, validation_calls: list[object],
) -> None:
    first, first_parameters = body_input_factory("first")
    second, second_parameters = body_input_factory("other")
    cache = integrity.CatalogBodyValidationCache(max_entries=1)
    integrity.validate_catalog_body(first, **first_parameters, cache=cache)
    integrity.validate_catalog_body(second, **second_parameters, cache=cache)
    integrity.validate_catalog_body(second, **second_parameters, cache=cache)
    integrity.validate_catalog_body(first, **first_parameters, cache=cache)
    assert len(validation_calls) == 3


@pytest.mark.parametrize("field", ["max_bytes", "max_entries"])
@pytest.mark.parametrize("value", [float("nan"), True, False, 1.0, 1.5])
def test_body_validation_rejects_non_integer_bounds(field: str, value: object) -> None:
    with pytest.raises(TypeError, match="必须是严格整数"), integrity.catalog_body_validation_scope(**{field: value}):
        pass


@pytest.mark.parametrize("bounds", [
    {"max_bytes": 8 * 1024 * 1024 + 1},
    {"max_entries": 4097},
])
def test_body_validation_rejects_bounds_above_fixed_ceiling(bounds: dict[str, int]) -> None:
    with pytest.raises(ValueError, match="不能超过硬上限"), integrity.catalog_body_validation_scope(**bounds):
        pass


@pytest.mark.parametrize("limit", ["bytes", "entries"])
def test_body_validation_evicts_old_success_when_either_bound_is_reached(
    body_input_factory: _BodyInputFactory, validation_calls: list[object], limit: str,
) -> None:
    first, first_parameters = body_input_factory("first")
    second, second_parameters = body_input_factory("other")
    bounds = {"max_bytes": max(len(first), len(second))} if limit == "bytes" else {"max_entries": 1}
    with integrity.catalog_body_validation_scope(**bounds):
        integrity.validate_catalog_body(first, **first_parameters)
        integrity.validate_catalog_body(second, **second_parameters)
        integrity.validate_catalog_body(second, **second_parameters)
        integrity.validate_catalog_body(first, **first_parameters)
    assert len(validation_calls) == 3


def test_body_validation_skips_oversize_success_without_evicting_small_success(
    body_input_factory: _BodyInputFactory, validation_calls: list[object],
) -> None:
    small, small_parameters = body_input_factory()
    large, large_parameters = body_input_factory(payload="x" * (2 * len(small)))
    with integrity.catalog_body_validation_scope(max_bytes=len(small)):
        integrity.validate_catalog_body(small, **small_parameters)
        for _ in range(2):
            integrity.validate_catalog_body(large, **large_parameters)
        integrity.validate_catalog_body(small, **small_parameters)
    assert len(validation_calls) == 3
