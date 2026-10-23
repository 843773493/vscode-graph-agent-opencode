from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def isolate_gateway_client_proxy_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """伪造 HTTP 请求不读取开发机代理设置。"""

    for name in (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
        "NO_PROXY",
        "no_proxy",
    ):
        monkeypatch.delenv(name, raising=False)
