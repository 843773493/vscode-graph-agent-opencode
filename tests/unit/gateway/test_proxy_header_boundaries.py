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
    GATEWAY_PROXY_DROPPED_HEADERS,
    HOP_BY_HOP_HEADERS,
    filter_hop_by_hop_headers,
)
from app.gateway.server.workspace_proxy import (
    _response_headers,
    _set_authoritative_header,
)

# 两条代理共用的 Gateway 凭据/目标选择剥离集合。任一代理只用逐跳集合、漏掉其中
# 一项，都会让客户端伪造的 workspace_id 或本地凭据漂到上游。
_EXPECTED_GATEWAY_DROPPED_HEADERS = frozenset(
    {
        "host",
        "x-request-id",
        "x-local-token",
        "x-boxteam-federation-token",
        "x-boxteam-workspace-id",
    }
)


def test_hop_by_hop_set_is_exactly_the_rfc7230_hop_headers() -> None:
    assert HOP_BY_HOP_HEADERS == frozenset(
        {
            "connection",
            "keep-alive",
            "proxy-authenticate",
            "proxy-authorization",
            "te",
            "trailer",
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


def test_filter_drops_headers_nominated_by_connection() -> None:
    """变异：漏掉 Connection 点名的头部，本用例必须红。

    RFC 7230 6.1 规定 Connection 列出的字段名也是逐跳的。只剔固定清单会让
    上游用来标注内部/私有含义的头部原样漂到浏览器侧。
    """
    filtered = filter_hop_by_hop_headers(
        [
            ("Connection", "keep-alive, x-internal-hop"),
            ("X-Internal-Hop", "MUST-BE-DROPPED"),
            ("X-Kept", "keep-me"),
        ]
    )

    assert filtered == {"X-Kept": "keep-me"}


def test_filter_drops_connection_nominated_headers_regardless_of_case() -> None:
    """Connection 值与被点名头部的大小写都不敏感。"""
    filtered = filter_hop_by_hop_headers(
        [
            ("connection", "X-Internal-Hop"),
            ("x-internal-hop", "MUST-BE-DROPPED"),
            ("X-Kept", "keep-me"),
        ]
    )

    assert filtered == {"X-Kept": "keep-me"}


def test_filter_drops_nominated_headers_from_every_connection_value() -> None:
    """点名可能分散在多条 Connection 里，必须全部收集。"""
    filtered = filter_hop_by_hop_headers(
        [
            ("Connection", "x-first"),
            ("Connection", "x-second"),
            ("X-First", "MUST-BE-DROPPED"),
            ("X-Second", "MUST-BE-DROPPED"),
            ("X-Kept", "keep-me"),
        ]
    )

    assert filtered == {"X-Kept": "keep-me"}


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


def test_gateway_proxy_dropped_header_set_is_exactly_the_shared_contract() -> None:
    """两条代理共用的凭据/目标选择剥离集合必须只此一份且逐项完整。"""

    from app.gateway.server.workspace_proxy import PROXY_ONLY_DROPPED_HEADERS

    assert GATEWAY_PROXY_DROPPED_HEADERS == _EXPECTED_GATEWAY_DROPPED_HEADERS
    # 工作区 API 代理只在其上追加会话加载策略与浏览器 Cookie。
    assert PROXY_ONLY_DROPPED_HEADERS == GATEWAY_PROXY_DROPPED_HEADERS | {
        "x-boxteam-history-loading",
        "cookie",
    }


@pytest.mark.parametrize("header", sorted(_EXPECTED_GATEWAY_DROPPED_HEADERS))
def test_both_proxies_never_forward_the_client_supplied_credential_header(
    header: str,
) -> None:
    """变异：任一条代理漏掉集合里的一项，本用例必须红。

    其中 x-request-id 与 x-local-token 会被代理用自己的权威值覆盖，因此这里断言
    「上游看不到客户端的 attacker 值」，而不是该键彻底不存在。
    """

    from app.gateway.auxiliary_proxy import _proxy_request_headers
    from app.gateway.server.workspace_proxy import _proxy_headers

    application = FastAPI()

    def build(builder) -> dict[str, str]:
        request = Request(
            {
                "type": "http",
                "method": "GET",
                "path": "/api/v1/workspace",
                "app": application,
                "headers": [(header.encode(), b"attacker"), (b"x-kept", b"ok")],
            }
        )
        request.state.request_id = "req_drop"
        return builder(request)

    for builder in (_proxy_headers, _proxy_request_headers):
        headers = build(builder)
        assert headers["x-kept"] == "ok"
        # 客户端值一旦没被剥离，代理再写权威值时就会留下两个同名键
        # （大小写可能不同）；按大小写不敏感计数才能真正发现泄漏。
        occurrences = [
            value for key, value in headers.items() if key.lower() == header
        ]
        assert occurrences != ["attacker"]
        assert len(occurrences) <= 1


def test_authoritative_header_replaces_upstream_case_variant() -> None:
    """变异：不先剔除同名条目，上游值会与本机权威值并存，本用例必须红。

    httpx 把上游响应头名统一小写；嵌套 Gateway 也会带自己的
    X-BoxTeam-Route-Revision。若直接赋值，响应里会出现两个仅大小写不同的同名
    头部，浏览器的大小写不敏感 get() 只读第一条，拿到的是上游值。
    """
    headers = {"x-boxteam-route-revision": "gw_stream:REMOTE", "content-type": "text/plain"}

    _set_authoritative_header(headers, "X-BoxTeam-Route-Revision", "gw_stream:LOCAL")

    assert headers["X-BoxTeam-Route-Revision"] == "gw_stream:LOCAL"
    assert [
        value
        for key, value in headers.items()
        if key.lower() == "x-boxteam-route-revision"
    ] == ["gw_stream:LOCAL"]
