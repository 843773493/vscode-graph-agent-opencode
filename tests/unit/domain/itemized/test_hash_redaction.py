"""class/length/session digest 的固定跨语言证据与真实 hash 入口验证。"""

import json
from copy import deepcopy
from dataclasses import replace
from pathlib import Path

import pytest

from app.domain.itemized.assembly_snapshot import ContextAssemblySnapshot
from app.domain.itemized.errors import ItemSchemaError
from app.domain.itemized.hashing import canonical_json_bytes, sha256_jcs
from app.domain.itemized.redaction import SessionHashRedactor, validate_hash_redaction
from app.domain.itemized.refs import ToolSetRef
from app.domain.itemized.request_hash import context_request_hash
from app.domain.itemized.request_plan import ContextRequestPlan
from app.domain.itemized.serialization import _hash_safe_value, normalize_wire_request


@pytest.fixture(scope="session")
def redaction_vectors() -> dict[str, object]:
    return json.loads(
        (Path.cwd() / "tests/fixtures/itemized/hash_redaction_vectors.json").read_text(
            encoding="utf-8"
        )
    )


@pytest.fixture
def redactor() -> SessionHashRedactor:
    # 只用于公开 golden 的测试 key，不访问真实 session 或运行环境。
    return SessionHashRedactor(session_key=bytes(32))


@pytest.mark.parametrize("index", range(6))
def test_session_redaction_matches_bun_golden(
    redaction_vectors: dict[str, object], index: int
) -> None:
    vector = redaction_vectors["vectors"][index]
    encoder = SessionHashRedactor(session_key=bytes.fromhex(vector["key_hex"]))
    marker = encoder.redact(vector["value"], redaction_class=vector["redaction_class"])
    assert canonical_json_bytes(vector["value"]).decode() == vector["canonical"]
    assert marker == vector["marker"]
    assert canonical_json_bytes(marker).decode() == vector["marker_canonical"]
    assert sha256_jcs(marker) == vector["marker_hash"]


def test_body_class_and_session_are_independently_bound(
    redaction_vectors: dict[str, object],
) -> None:
    markers = [row["marker"] for row in redaction_vectors["vectors"][:4]]
    assert len({sha256_jcs(marker) for marker in markers}) == 4
    assert len({marker["content_length"] for marker in markers}) == 1
    # class 在外层 preimage 绑定；HMAC 本身遵循已有 JCS(value) 合同。
    assert markers[0]["redacted_stable_digest"] == markers[3]["redacted_stable_digest"]
    assert markers[0]["redacted_stable_digest"] != markers[1]["redacted_stable_digest"]


def test_paths_are_explicit_and_do_not_mutate_wire(
    redactor: SessionHashRedactor,
) -> None:
    wire = {"messages": [{"content": "private-body"}], "api_key": "secret-key"}
    original = deepcopy(wire)
    safe = redactor.redact_paths(
        wire,
        classes={
            ("messages", 0, "content"): "private.context",
            ("api_key",): "credential.api_key",
        },
    )
    assert wire == original
    assert safe == normalize_wire_request(safe)
    encoded = canonical_json_bytes(safe)
    assert b"private-body" not in encoded and b"secret-key" not in encoded
    assert repr(redactor) == "SessionHashRedactor()"
    assert redactor.redact(
        wire, redaction_class="private.context"
    ) == SessionHashRedactor(session_key=bytes(32)).redact(
        wire, redaction_class="private.context"
    )


@pytest.mark.parametrize(
    "classes",
    [
        {("missing",): "private.context"},
        {(): "private.context", ("body",): "private.context"},
        {(True,): "private.context"},
        {(-1,): "private.context"},
        {"body": "private.context"},
    ],
)
def test_missing_overlapping_or_invalid_paths_fail(
    redactor: SessionHashRedactor, classes: object
) -> None:
    with pytest.raises(ItemSchemaError, match="hash-redaction-path-invalid"):
        redactor.redact_paths({"body": "secret"}, classes=classes)


@pytest.mark.parametrize("key", [None, b"", bytes(31), "a" * 32, bytearray(32)])
def test_key_is_required_and_never_defaulted(key: object) -> None:
    with pytest.raises(ItemSchemaError, match="hash-redaction-key-required"):
        SessionHashRedactor(session_key=key)


@pytest.mark.parametrize(
    "value", [float("nan"), float("inf"), "\ud800", {"\udfff": 1}, {1: "secret"}]
)
def test_redaction_rejects_invalid_jcs_input(
    redactor: SessionHashRedactor, value: object
) -> None:
    with pytest.raises(ItemSchemaError):
        redactor.redact(value, redaction_class="private.context")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("redaction_class", ""),
        ("redaction_class", "private body"),
        ("redaction_class", None),
        ("content_length", True),
        ("content_length", -1),
        ("content_length", "12"),
        ("redacted_stable_digest", "sha256:jcs:v1:" + "a" * 64),
        ("redacted_stable_digest", "hmac-sha256:session:v1:" + "A" * 64),
        ("redacted_stable_digest", "hmac-sha256:session:v1:" + "a" * 63),
        ("body", "secret"),
    ],
)
def test_malformed_markers_fail_closed(
    redactor: SessionHashRedactor, field: str, value: object
) -> None:
    marker = redactor.redact("secret", redaction_class="private.context")
    marker[field] = value
    with pytest.raises(ItemSchemaError, match="hash-redaction-invalid"):
        _hash_safe_value(marker)


@pytest.mark.parametrize(
    "field", ["redaction_class", "content_length", "redacted_stable_digest"]
)
def test_marker_requires_all_three_fields(
    redactor: SessionHashRedactor, field: str
) -> None:
    marker = redactor.redact("secret", redaction_class="private.context")
    marker.pop(field)
    with pytest.raises(ItemSchemaError, match="hash-redaction-invalid"):
        validate_hash_redaction(marker)


@pytest.mark.parametrize(
    "raw", ["actual-secret", "<redacted>", None, {"redacted": True}]
)
def test_credentials_cannot_collapse_to_a_constant_marker(raw: object) -> None:
    with pytest.raises(ItemSchemaError, match="hash-redaction"):
        normalize_wire_request({"api_key": raw})


def test_token_limits_are_semantic_but_auth_headers_are_excluded() -> None:
    first = normalize_wire_request(
        {"max_tokens": 100, "headers": {"api_key": "secret"}}
    )
    second = normalize_wire_request(
        {"max_tokens": 101, "headers": {"api_key": "other"}}
    )
    assert first == {"max_tokens": 100}
    assert sha256_jcs(first) != sha256_jcs(second)


def test_real_plan_and_request_hash_bind_sensitive_markers(
    redactor: SessionHashRedactor,
    golden_plan: ContextRequestPlan,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[object] = []

    def capture(value: object) -> str:
        captured.append(value)
        return sha256_jcs(value)

    monkeypatch.setattr("app.domain.itemized.request_hash.sha256_jcs", capture)
    requests: list[str] = []
    plans: list[str] = []
    for body in ("secret-a", "secret-b"):
        marker = redactor.redact(body, redaction_class="credential.api_key")
        wire = {"messages": [{"content": marker}], "model": "model-1"}
        requests.append(
            context_request_hash(golden_plan, "provider-1", wire_request=wire)
        )
        tool = ToolSetRef.from_tool_snapshot(
            session_id=golden_plan.session_id,
            snapshot_id="tool-sensitive",
            plan_id="plan-sensitive",
            source_revision="r1",
            tools=[{"name": "query"}],
            tool_policy={"api_key": marker},
        )
        plans.append(
            ContextRequestPlan(
                session_id=golden_plan.session_id,
                plan_id="plan-sensitive",
                refs=(),
                tool_set_refs=(tool,),
            ).plan_hash()
        )
    assert requests[0] != requests[1] and plans[0] != plans[1]
    assert len(captured) == 2
    assert all(b"secret-" not in canonical_json_bytes(value) for value in captured)
    assert (
        captured[0]["wire_request"]["messages"][0]["content"]["redaction_class"]
        == "credential.api_key"
    )


def test_marker_snapshot_restores_without_original_body_or_key(
    redactor: SessionHashRedactor,
    omitted_snapshot: ContextAssemblySnapshot,
) -> None:
    wire = redactor.redact_paths(
        {"messages": [{"content": "private-body"}]},
        classes={
            ("messages", 0, "content"): "private.context",
        },
    )
    plan = ContextRequestPlan(
        session_id=omitted_snapshot.session_id,
        plan_id=omitted_snapshot.plan_id,
        refs=omitted_snapshot.refs,
        selection=omitted_snapshot.selection,
        assembly_id=omitted_snapshot.assembly_id,
        plan_state="sealed",
    )
    snapshot = replace(
        omitted_snapshot,
        request_hash_preimage=wire,
        request_hash=context_request_hash(
            plan,
            "provider-1",
            target_format="native",
            wire_request=wire,
        ),
    )
    raw = json.loads(canonical_json_bytes(snapshot.to_dict()))
    restored = ContextAssemblySnapshot.from_dict(raw)
    restored.validate_hashes()
    assert restored == snapshot
    assert "private-body" not in str(raw)
