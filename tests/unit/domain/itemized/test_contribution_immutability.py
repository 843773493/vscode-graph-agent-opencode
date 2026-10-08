from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from types import MappingProxyType

from app.domain.itemized.hashing import canonical_json_bytes, contribution_content_hash
from app.domain.itemized.request_plan import ContextContribution
from app.domain.itemized.serde.registry import parse_contribution


def _contribution(body: object, extensions: Mapping[str, object]) -> ContextContribution:
    return ContextContribution(
        contribution_id="contribution-1",
        source_kind="prompt",
        source_revision="revision-1",
        content_hash=contribution_content_hash("prompt", body),
        body=body,
        extensions=extensions,
    )


def test_body_and_extensions_are_isolated_on_input_and_read() -> None:
    body = {"parts": [{"text": "before"}]}
    extensions: dict[str, object] = {
        "vendor.annotation": {
            "extension_version": "1",
            "protection": "public",
            "value": {"labels": ["first"]},
        }
    }
    contribution = _contribution(body, extensions)
    frozen_body = {"parts": [{"text": "before"}]}
    frozen_extensions = {
        "vendor.annotation": {
            "extension_version": "1",
            "protection": "public",
            "value": {"labels": ["first"]},
        }
    }

    body["parts"][0]["text"] = "mutated input"
    extensions["vendor.annotation"]["value"]["labels"].append("mutated input")
    assert contribution.body == frozen_body
    assert contribution.extensions == frozen_extensions
    assert contribution.content_hash == contribution_content_hash("prompt", frozen_body)
    assert contribution.content_length == len(canonical_json_bytes(frozen_body))

    contribution.body["parts"][0]["text"] = "mutated read"
    contribution.extensions["vendor.annotation"]["value"]["labels"].append("mutated read")
    assert contribution.body == frozen_body
    assert contribution.extensions == frozen_extensions


def test_tuple_values_keep_their_type_while_reads_are_isolated() -> None:
    body = {"parts": ("one", ("two",))}
    contribution = _contribution(body, {})

    body["parts"] = body["parts"] + ("changed input",)
    read = contribution.body
    read["parts"] = read["parts"] + ("changed read",)

    assert contribution.body == {"parts": ("one", ("two",))}


def test_mapping_proxy_body_and_extensions_keep_json_contracts() -> None:
    body_values = {"labels": ["body-before"]}
    body = MappingProxyType(
        {
            "nested": MappingProxyType(body_values),
            "tuple": ("first", ("second",)),
        }
    )
    extension_labels = {"labels": ["extension-before"]}
    extensions = MappingProxyType(
        {
            "vendor.annotation": MappingProxyType(
                {
                    "extension_version": "1",
                    "protection": "public",
                    "value": MappingProxyType(
                        {"nested": MappingProxyType(extension_labels)}
                    ),
                }
            )
        }
    )
    expected_body = {
        "nested": {"labels": ["body-before"]},
        "tuple": ("first", ("second",)),
    }
    expected_extensions = {
        "vendor.annotation": {
            "extension_version": "1",
            "protection": "public",
            "value": {"nested": {"labels": ["extension-before"]}},
        }
    }
    contribution = _contribution(body, extensions)
    expected_hash = contribution_content_hash("prompt", expected_body)

    body_values["labels"].append("mutated input")
    extension_labels["labels"].append("mutated input")
    assert contribution.body == expected_body
    assert contribution.extensions == expected_extensions
    assert contribution.content_hash == expected_hash
    assert contribution.content_length == len(canonical_json_bytes(expected_body))

    contribution.body["nested"]["labels"].append("mutated read")
    contribution.extensions["vendor.annotation"]["value"]["nested"]["labels"].append(
        "mutated read"
    )
    assert contribution.body == expected_body
    assert contribution.extensions == expected_extensions

    manifest = contribution.binding_manifest()
    manifest["extensions"]["vendor.annotation"]["value"]["nested"]["labels"].append(
        "mutated manifest"
    )
    updated = replace(contribution, source_revision="revision-2")
    assert updated.body == expected_body
    assert updated.extensions == expected_extensions
    serialized = contribution.to_dict()
    restored = parse_contribution(serialized, sealed=False)
    assert canonical_json_bytes(restored.to_dict()) == canonical_json_bytes(serialized)
    assert restored.body == expected_body
    assert restored.body["tuple"] == ("first", ("second",))


def test_replace_binding_manifest_and_serde_preserve_nested_json() -> None:
    body = {"parts": [{"text": "stable"}, {"image": {"url": "x"}}]}
    extensions = {
        "vendor.annotation": {
            "extension_version": "1",
            "protection": "public",
            "value": {"labels": ["stable"]},
        }
    }
    contribution = _contribution(body, extensions)

    manifest = contribution.binding_manifest()
    manifest["extensions"]["vendor.annotation"]["value"]["labels"].append("changed")
    projected = contribution.to_dict()
    projected["body"]["parts"][0]["text"] = "changed"
    projected["extensions"]["vendor.annotation"]["value"]["labels"].append("changed")
    assert contribution.body == body
    assert contribution.extensions == extensions

    updated = replace(contribution, source_revision="revision-2")
    assert updated.source_revision == "revision-2"
    assert updated.body == body
    assert updated.extensions == extensions
    serialized = contribution.to_dict()
    restored = parse_contribution(serialized, sealed=False)
    assert canonical_json_bytes(restored.to_dict()) == canonical_json_bytes(serialized)
    assert restored.body == body
    assert restored.extensions == extensions
    assert "_body_snapshot" not in serialized
    assert "_extensions_snapshot" not in serialized
