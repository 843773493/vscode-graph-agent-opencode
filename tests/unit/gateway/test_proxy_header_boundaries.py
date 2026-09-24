"""冻结工作区 API 与辅助服务代理共用同一份逐跳头部收敛实现。

两处代理原本逐字重复一份 8 项逐跳头部集合与同一个过滤表达式；这里锁住收敛后
的唯一实现，并对「少剔一个头部」的变异给出可观测的红。
"""

from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI
from starlette.requests import Request

from app.gateway.auxiliary_proxy import _proxy_response_headers
from app.gateway.proxy_upstream import (
    HOP_BY_HOP_HEADERS,
    filter_hop_by_hop_headers,
)
from app.gateway.server.workspace_proxy import _response_headers


def test_hop_by_hop_set_is_exactly_the_rfc7230_hop_headers() -> None:
    assert HOP_BY_HOP_HEADERS == frozenset(
        {
            "connection",
            "keep-alive",
            "proxy-authenticate",
            "proxy-authorization",
            "te",
            "trailers",
            "transfer-encoding",
            "upgrade",
        }
    )


@pytest.mark.parametrize("header", sorted(HOP_BY_HOP_HEADERS))
def test_filter_drops_each_hop_header_regardless_of_case(header: str) -> None:
    """变异：任一逐跳头部漏剔都必须让本用例红。"""
    assert filter_hop_by_hop_headers([(header, "v")]) == {}
    assert filter_hop_by_hop_headers([(header.upper(), "v")]) == {}


def test_filter_keeps_end_to_end_headers_in_inbound_order() -> None:
    filtered = filter_hop_by_hop_headers(
        [("Content-Type", "application/json"), ("X-Trace", "1")]
    )

    assert list(filtered) == ["Content-Type", "X-Trace"]


def _upstream_response_with(header: str) -> httpx.Response:
    return httpx.Response(
        status_code=200,
        headers=[(header, "v"), ("Content-Type", "text/plain")],
        content=b"ok",
    )


@pytest.mark.parametrize("header", sorted(HOP_BY_HOP_HEADERS))
def test_both_proxies_apply_the_shared_response_filter(header: str) -> None:
    """工作区 API 与辅助服务代理必须等价剔除同一组逐跳响应头部。"""
    workspace_proxy_headers = _response_headers(_upstream_response_with(header))
    auxiliary_proxy_headers = _proxy_response_headers(
        _upstream_response_with(header)
    )

    assert header not in {key.lower() for key in workspace_proxy_headers}
    assert header not in {key.lower() for key in auxiliary_proxy_headers}
    assert workspace_proxy_headers == auxiliary_proxy_headers


def test_gateway_proxy_headers_drop_hop_and_credential_headers() -> None:
    """工作区代理在逐跳头部之外还必须剥离 Gateway 凭据与浏览器 Cookie。"""
    from app.gateway.server.workspace_proxy import _proxy_headers

    application = FastAPI()
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/v1/workspace",
            "app": application,
            "headers": [
                (b"connection", b"keep-alive"),
                (b"x-local-token", b"attacker"),
                (b"x-boxteam-workspace-id", b"gw_attacker"),
                (b"cookie", b"session=1"),
                (b"x-end-to-end", b"kept"),
            ],
        }
    )
    request.state.request_id = "req_headers"

    headers = _proxy_headers(request)

    assert headers["x-end-to-end"] == "kept"
    assert "connection" not in {key.lower() for key in headers}
    assert "cookie" not in {key.lower() for key in headers}
    assert headers["X-Local-Token"] == "local-dev-token"
    assert "X-BoxTeam-Workspace-Id" not in headers
