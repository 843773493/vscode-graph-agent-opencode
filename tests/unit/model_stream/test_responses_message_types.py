from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest

from app.testing.model_stream.matcher import request_summary, safe_request_match_fields


@pytest.fixture
def responses_request_factory() -> Callable[[object], httpx.Request]:
    def create(body: object) -> httpx.Request:
        return httpx.Request(
            "POST",
            "https://example.test/v1/responses",
            json=body,
        )

    return create


def test_omitted_message_type_is_summarized_without_exposing_content(
    responses_request_factory: Callable[[object], httpx.Request],
) -> None:
    request = responses_request_factory(
        {
            "model": "test-model",
            "stream": True,
            "input": [
                {"role": "system", "content": "private instruction"},
                {
                    "role": "user",
                    "content": [{"type": "input_text", "text": "private prompt"}],
                },
            ],
        }
    )

    summary = request_summary(request)

    assert summary.selected_body["input_types"] == ["message", "message"]
    assert safe_request_match_fields(request) == {
        "model": "test-model",
        "stream": True,
        "input_types": ["message", "message"],
    }
    assert "input" not in summary.selected_body
    assert "private instruction" not in repr(summary)
    assert "private prompt" not in repr(summary)


def test_explicit_types_and_malformed_omitted_type_items_keep_existing_summary(
    responses_request_factory: Callable[[object], httpx.Request],
) -> None:
    request = responses_request_factory(
        {
            "input": [
                {"type": "function_call", "role": "user", "content": "text"},
                {"type": "", "role": "user", "content": "text"},
                {"type": None, "role": "user", "content": "text"},
                {"role": "unknown", "content": "text"},
                {"role": "user", "content": None},
                {"role": "user", "content": {"text": "text"}},
                {"role": "user", "content": [None]},
                {"role": "user", "content": [{"type": "input_text"}]},
                {"role": "user", "content": [{"type": "output_text", "text": "text"}]},
                None,
            ],
        }
    )

    assert request_summary(request).selected_body["input_types"] == [
        "function_call",
        "",
        "<unknown>",
        "<unknown>",
        "<unknown>",
        "<unknown>",
        "<unknown>",
        "<unknown>",
        "<unknown>",
        "<unknown>",
    ]


def test_image_and_file_blocks_require_valid_detail_and_references(
    responses_request_factory: Callable[[object], httpx.Request],
) -> None:
    request = responses_request_factory(
        {
            "input": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_image",
                            "detail": "auto",
                            "image_url": "private-image-reference",
                        }
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_image",
                            "detail": "high",
                            "file_id": "private-image-id",
                        }
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {"type": "input_file", "file_url": "private-file-reference"}
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {"type": "input_file", "file_data": "private-file-data"}
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {"type": "input_image", "detail": ["auto"], "image_url": "ref"}
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {"type": "input_image", "detail": {"value": "auto"}, "image_url": "ref"}
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {"type": "input_image", "detail": "auto", "image_url": {"url": "ref"}}
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {"type": "input_file", "file_id": ["ref"]}
                    ],
                },
            ],
        }
    )

    summary = request_summary(request)

    assert summary.selected_body["input_types"] == [
        "message",
        "message",
        "message",
        "message",
        "<unknown>",
        "<unknown>",
        "<unknown>",
        "<unknown>",
    ]
    assert "private-image-reference" not in repr(summary)
    assert "private-image-id" not in repr(summary)
    assert "private-file-reference" not in repr(summary)
    assert "private-file-data" not in repr(summary)
