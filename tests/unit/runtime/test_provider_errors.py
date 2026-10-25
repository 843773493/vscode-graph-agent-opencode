from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.runtime import litellm_runtime
from app.runtime.provider_errors import (
    is_transient_provider_error,
    mark_provider_failure,
)


class _ProviderStatusError(Exception):
    def __init__(self, status_code: int) -> None:
        super().__init__(f"HTTP {status_code}")
        self.status_code = status_code


def test_transient_provider_status_skips_sdk_loading(monkeypatch: pytest.MonkeyPatch) -> None:
    def reject_sdk_load() -> None:
        pytest.fail("明确可重试的 HTTP 状态不应加载 LiteLLM")

    monkeypatch.setattr(litellm_runtime, "load_litellm", reject_sdk_load)

    assert is_transient_provider_error(mark_provider_failure(_ProviderStatusError(503)))


def test_non_transient_provider_status_is_not_retryable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sdk_error = type("SdkError", (Exception,), {})
    monkeypatch.setattr(
        litellm_runtime,
        "load_litellm",
        lambda: SimpleNamespace(
            APIConnectionError=sdk_error,
            BadGatewayError=sdk_error,
            InternalServerError=sdk_error,
            RateLimitError=sdk_error,
            ServiceUnavailableError=sdk_error,
            Timeout=sdk_error,
        ),
    )
    assert not is_transient_provider_error(
        mark_provider_failure(_ProviderStatusError(400))
    )
    assert is_transient_provider_error(mark_provider_failure(sdk_error()))
