from __future__ import annotations

import json
import struct
from dataclasses import replace
from decimal import Decimal

import pytest

from app.domain.itemized import plan_hash, request_hash
from app.domain.itemized.errors import ItemSchemaError
from app.domain.itemized.hashing import (
    canonical_json_bytes,
    content_hash,
    contribution_content_hash,
    payload_content_length,
    sha256_jcs,
)
from app.domain.itemized.records import CanonicalItemRecord
from app.domain.itemized.refs import ContextRef
from app.domain.itemized.request_plan import ContextRequestPlan


@pytest.mark.parametrize("input_kind", ["binary64", "json", "integer"])
def test_shared_jcs_vectors(hash_vectors: dict[str, object], input_kind: str) -> None:
    rows = [
        row for row in hash_vectors["serialization"] if row["input_kind"] == input_kind
    ]
    assert rows
    for row in rows:
        if input_kind == "binary64":
            value = struct.unpack(">d", bytes.fromhex(row["input"]))[0]
        elif input_kind == "integer":
            value = int(row["input"])
        else:
            value = json.loads(row["input"])
        if row.get("error"):
            with pytest.raises(ItemSchemaError):
                canonical_json_bytes(value)
            continue
        expected = row["canonical"].encode("utf-8")
        assert canonical_json_bytes(value) == expected, row["id"]
        assert sha256_jcs(value) == row["hash"], row["id"]
        # 模拟重启后默认 JSON parser 的数值类型选择，而非保留旧进程 float。
        assert canonical_json_bytes(json.loads(expected)) == expected, row["id"]


@pytest.mark.parametrize(
    "family",
    [
        "content",
        "content-text",
        "contribution",
        "tool-manifest",
        "plan",
        "request",
        "idempotency",
        "legacy-message",
        "legacy-seed",
    ],
)
def test_shared_hash_preimages(
    hash_vectors: dict[str, object],
    golden_plan: ContextRequestPlan,
    monkeypatch: pytest.MonkeyPatch,
    family: str,
) -> None:
    row = next(row for row in hash_vectors["preimages"] if row["id"] == family)
    preimage = row["preimage"]
    assert canonical_json_bytes(preimage) == row["canonical"].encode("utf-8")
    assert sha256_jcs(preimage) == row["hash"]
    captured: list[bytes] = []

    def observe(value: object) -> str:
        captured.append(canonical_json_bytes(value))
        return sha256_jcs(value)

    if family.startswith("content"):
        actual = content_hash(preimage["payload_kind"], preimage["payload"])
    elif family == "contribution":
        actual = contribution_content_hash(
            preimage["contribution_kind"], preimage["body"]
        )
    elif family == "tool-manifest":
        actual = golden_plan.tool_set_refs[0].content_hash
        golden_plan.tool_set_refs[0].validate_manifest()
    elif family == "plan":
        monkeypatch.setattr(plan_hash, "sha256_jcs", observe)
        actual = golden_plan.plan_hash()
    elif family == "request":
        monkeypatch.setattr(request_hash, "sha256_jcs", observe)
        scenario = hash_vectors["scenario"]
        actual = request_hash.context_request_hash(
            golden_plan,
            scenario["provider"],
            projector_id=scenario["projector_id"],
            projector_version=scenario["projector_version"],
            target_format=scenario["target_format"],
            wire_request=scenario["wire_request"],
        )
    else:
        # 这里只证明合同 preimage 的 serializer；storage/migration 的接入另验。
        actual = sha256_jcs(preimage)
    assert actual == row["hash"], family
    if family in {"plan", "request"}:
        assert captured == [row["canonical"].encode("utf-8")]


def test_item_recovery_keeps_the_same_content_preimage(
    hash_vectors: dict[str, object],
) -> None:
    item = CanonicalItemRecord.from_dict(hash_vectors["scenario"]["item"])
    restored = CanonicalItemRecord.from_dict(
        json.loads(canonical_json_bytes(item.to_dict()))
    )
    assert restored == item
    changed_envelope = replace(
        item,
        item_id="another-item",
        item_sequence=2,
        status="completed",
        created_at="2027-01-01T00:00:00Z",
        metadata={"other": "metadata"},
        producer_ref={"producer_kind": "user", "producer_id": "other-ingress"},
    )
    assert changed_envelope.content_hash == item.content_hash


@pytest.mark.parametrize(
    "value", [{1: "bad key"}, b"bytes", {1, 2}, Decimal("0.1"), object()]
)
def test_non_json_values_are_rejected(value: object) -> None:
    with pytest.raises(ItemSchemaError):
        canonical_json_bytes(value)


def test_request_normalization_cannot_coerce_non_string_keys(
    golden_plan: ContextRequestPlan,
) -> None:
    with pytest.raises(ItemSchemaError, match="key"):
        request_hash.context_request_hash(
            golden_plan, "provider", wire_request={1: "value"}
        )


def test_text_hash_preserves_whitespace_normalization_and_array_order() -> None:
    for left, right in [("é", "e\u0301"), ("text", " text"), ("a\nb", "a\r\nb")]:
        assert content_hash("text", left) != content_hash("text", right)
    assert sha256_jcs([1, 2]) != sha256_jcs([2, 1])
    with pytest.raises(ItemSchemaError, match="Unicode"):
        payload_content_length("text", "\ud800")


def test_legacy_seed_contract_binds_message_hash_and_source_coordinate(
    hash_vectors: dict[str, object],
) -> None:
    rows = {row["id"]: row for row in hash_vectors["preimages"]}
    message, seed = rows["legacy-message"], rows["legacy-seed"]
    assert seed["preimage"]["legacy_message_hash"] == message["hash"]
    for field, value in [
        ("role", "assistant"),
        ("message_id", "other-id"),
        ("source_session_id", "other-session"),
    ]:
        changed_message_hash = sha256_jcs({**message["preimage"], field: value})
        assert (
            sha256_jcs(
                {**seed["preimage"], "legacy_message_hash": changed_message_hash}
            )
            != seed["hash"]
        )


def test_plan_identity_and_provider_profile_have_separate_hash_scopes(
    golden_plan: ContextRequestPlan,
) -> None:
    assert (
        ContextRequestPlan(
            session_id=golden_plan.session_id, plan_id="one", refs=()
        ).plan_hash()
        == ContextRequestPlan(
            session_id=golden_plan.session_id, plan_id="two", refs=()
        ).plan_hash()
    )
    assert (
        replace(golden_plan, plan_creation_idempotency_key="other-key").plan_hash()
        == golden_plan.plan_hash()
    )
    assert request_hash.context_request_hash(
        golden_plan, "provider-a"
    ) != request_hash.context_request_hash(golden_plan, "provider-b")
    for changes in (
        {"history_view_revision": 4},
        {"active_view_id": "other-view"},
        {"selection_policy": "other-policy"},
    ):
        assert replace(golden_plan, **changes).plan_hash() != golden_plan.plan_hash()


def test_plan_registry_order_does_not_override_selection_order(
    golden_plan: ContextRequestPlan,
    hash_vectors: dict[str, object],
) -> None:
    source = CanonicalItemRecord.from_dict(hash_vectors["scenario"]["item"])
    second_ref = ContextRef.canonical_item(
        replace(source, item_id="item-second", item_sequence=2),
        session_id=golden_plan.session_id, thread_id="thread-1",
    )
    second_entry = replace(golden_plan.selection[0], ref=second_ref, plan_ordinal=2)
    selected = replace(
        golden_plan,
        refs=(*golden_plan.refs, second_ref),
        selection=(*golden_plan.selection, second_entry),
    )
    reordered_registry = replace(selected, refs=tuple(reversed(selected.refs)))
    assert reordered_registry.plan_hash() == selected.plan_hash()
    assert request_hash.context_request_hash(
        reordered_registry, "provider"
    ) == request_hash.context_request_hash(selected, "provider")
    reordered_selection = tuple(
        replace(entry, plan_ordinal=ordinal)
        for ordinal, entry in enumerate(reversed(selected.selection))
    )
    assert (
        replace(selected, selection=reordered_selection).plan_hash()
        != selected.plan_hash()
    )
    # 未选中的 registry 候选不影响已经冻结的语义计划。
    assert (
        replace(golden_plan, refs=(*golden_plan.refs, second_ref)).plan_hash()
        == golden_plan.plan_hash()
    )


def test_request_hash_excludes_transport_and_binds_model_and_wire_content(
    golden_plan: ContextRequestPlan,
    hash_vectors: dict[str, object],
) -> None:
    wire = hash_vectors["scenario"]["wire_request"]
    baseline = request_hash.context_request_hash(
        golden_plan, "provider", wire_request=wire
    )
    volatile = {
        "headers": {"authorization": "test-fixture-secret"},
        "request_id": "other",
        "attempt": 2,
        "timestamp": "later",
    }
    assert (
        request_hash.context_request_hash(
            golden_plan, "provider", wire_request={**wire, **volatile}
        )
        == baseline
    )
    for changes in ({"model": "other-model"}, {"messages": []}, {"tools": []}):
        assert (
            request_hash.context_request_hash(
                golden_plan, "provider", wire_request={**wire, **changes}
            )
            != baseline
        )


def test_jcs_is_order_independent_and_rejects_non_finite_values() -> None:
    assert canonical_json_bytes({"b": 2, "a": 1}) == b'{"a":1,"b":2}'
    assert canonical_json_bytes({"a": [True, None, "中文"]}).startswith(b'{"a":')
    with pytest.raises(ItemSchemaError, match="非有限浮点数"):
        canonical_json_bytes({"value": float("nan")})


def test_hash_helpers_reject_non_string_payload_kind() -> None:
    with pytest.raises(ItemSchemaError, match="payload_kind"):
        content_hash(1, "payload")
    with pytest.raises(ItemSchemaError, match="payload_kind"):
        payload_content_length(1, "payload")


def test_jcs_golden_vectors_freeze_bytes_numbers_and_utf16_key_order() -> None:
    value = {"z": -0.0, "exp": 1e21, "small": 1e-7, "unicode": "é/中文"}
    assert canonical_json_bytes(value) == (
        b'{"exp":1e+21,"small":1e-7,"unicode":"\xc3\xa9/'
        b'\xe4\xb8\xad\xe6\x96\x87","z":0}'
    )
    assert sha256_jcs(value) == (
        "sha256:jcs:v1:0e563d599a5c9bc84abc572c677ba8e46344173ea4e1f3a529583547dd5f38c8"
    )
    # RFC 8785 按 UTF-16 code unit 排序；非 BMP key 不能按 Python code point
    # 的直觉顺序替代，否则跨语言恢复会得到不同 preimage。
    utf16_order = {"\U00010000": 1, "\uffff": 2, "a": 3}
    assert (
        canonical_json_bytes(utf16_order)
        == b'{"a":3,"\xf0\x90\x80\x80":1,"\xef\xbf\xbf":2}'
    )
    assert sha256_jcs(utf16_order) == (
        "sha256:jcs:v1:fe13f01b100da846fe864aa07d9df7cfe9ec8499fd0638967cbfdb33fef91321"
    )


@pytest.mark.parametrize("value", [float("inf"), float("-inf"), float("nan")])
def test_jcs_golden_vectors_reject_all_non_finite_numbers(value: float) -> None:
    with pytest.raises(ItemSchemaError, match="非有限浮点数"):
        canonical_json_bytes({"value": value})


def test_contribution_hash_is_kind_sensitive() -> None:
    body = {"value": "same"}
    assert contribution_content_hash("prompt", body) != contribution_content_hash(
        "notice", body
    )
