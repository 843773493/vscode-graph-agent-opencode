from __future__ import annotations

import pytest

from app.gateway.proxy_upstream import build_upstream_url


@pytest.mark.parametrize(
    ("label", "path"),
    [
        # 单层编码：uvicorn 解一层成 ..，httpx 折叠点段。
        ("pct_single", "../../api/gateway/health"),
        # 双层编码：uvicorn 解一层成字面量 %2e%2e，httpx 不折叠，上游会再解一层。
        ("pct_double", "%2e%2e/%2e%2e/api/gateway/health"),
        # 三层编码。
        ("pct_triple", "%252e%252e/%252e%252e/api/gateway/health"),
        ("pct_quad", "%25252e%25252e/%25252e%25252e/api/gateway/health"),
        # 大写十六进制。
        ("pct_upper", "%2E%2E/%2E%2E/api/gateway/health"),
        ("pct_upper_mixed", "%2E%2e/%2E%2e/api/gateway/health"),
        # 编码斜杠。
        ("pct_encoded_slash", "%2e%2e%2f%2e%2e%2fapi/gateway/health"),
        ("dot_then_encoded_slash", "..%2f..%2fapi/gateway/health"),
        ("double_encoded_slash", "%252e%252e%252f%252e%252e%252fapi/gateway/health"),
        # 明文点段。
        ("plain_dotdot", "../../api/gateway/health"),
        ("mixed_real_and_encoded", "a/../../api/gateway/health"),
    ],
)
def test_build_upstream_url_rejects_any_encoding_form_that_escapes_namespace(
    label: str,
    path: str,
) -> None:
    """任何编码层次下的 .. 点段都必须响亮失败，不能让脏路径继续转发。

    这些输入是 uvicorn 单层解码后真实会传进路由处理函数的形态：单层编码已成 ..，
    双层及以上仍是百分号编码。守卫若不归一就判断归属，%2e%2e 会因 httpx 不再二次
    解码而通过前缀校验，上游（同为 uvicorn）再解一层就逃出命名空间。
    """

    with pytest.raises(ValueError) as captured:
        build_upstream_url(
            "http://127.0.0.1:41001",
            ("api", "v1"),
            path,
        )

    assert "越出上游命名空间" in str(captured.value) or "点段" in str(captured.value)


@pytest.mark.parametrize(
    ("label", "path", "expected_path"),
    [
        # 含合法百分号编码的路径必须继续放行，这是本修复最易误杀的地方。
        ("space_in_name", "files/a%20b.txt", "/api/v1/files/a b.txt"),
        (
            "non_ascii_name",
            "files/%E4%B8%AD%E6%96%87.txt",
            "/api/v1/files/\u4e2d\u6587.txt",
        ),
        # 编码斜杠：业务里真实出现过，分段语义不能被改写。
        ("encoded_slash", "files/docs%2Fa.png", "/api/v1/files/docs/a.png"),
        # 字面量百分号编码为 %25，解码后仍含 %，不能因此被拒。
        ("literal_percent", "files/100%25.txt", "/api/v1/files/100%.txt"),
        # 单层编码的点，不是点段。
        ("encoded_dot_in_name", "files/a%2Eb.txt", "/api/v1/files/a.b.txt"),
        ("plain_segment", "workspace", "/api/v1/workspace"),
        ("dot_segment_only", "files/./a.txt", "/api/v1/files/a.txt"),
        ("plus_is_literal", "files/a+b.txt", "/api/v1/files/a+b.txt"),
    ],
)
def test_build_upstream_url_keeps_legitimate_encoded_paths(
    label: str,
    path: str,
    expected_path: str,
) -> None:
    """合法编码路径必须继续放行：不动点归一只看是否真的出现 .. 点段。"""

    url = build_upstream_url(
        "http://127.0.0.1:41001",
        ("api", "v1"),
        path,
    )

    assert url.path == expected_path


def test_build_upstream_url_terminates_on_degenerate_encoding() -> None:
    """畸形/退化编码不能造成无限解码或异常：归一必须收敛。"""

    for path in (
        "files/%",
        "files/%zz",
        "files/%2",
        "files/%2525",
        "files/trailing%",
        "files/" + "%25" * 200 + "x.txt",
    ):
        url = build_upstream_url(
            "http://127.0.0.1:41001",
            ("api", "v1"),
            path,
        )
        assert url.path.startswith("/api/v1/")


@pytest.mark.parametrize(
    ("label", "path", "expected_raw_path"),
    [
        # 文件名里的 # 与 ? 由客户端编码成 %23/%3F；uvicorn 解一层后路由参数
        # 就是含字面量 #/? 的形态，httpx 会直接拒绝这样的 path。
        ("hash_in_name", "files/a#b.txt", b"/api/v1/files/a%23b.txt"),
        ("question_in_name", "files/a?b.txt", b"/api/v1/files/a%3Fb.txt"),
        # 其余字符的线上形态必须与修复前逐字节一致。
        ("plain", "files/plain.txt", b"/api/v1/files/plain.txt"),
        ("space", "files/a b.txt", b"/api/v1/files/a%20b.txt"),
        ("literal_percent", "files/100%.txt", b"/api/v1/files/100%.txt"),
        ("encoded_slash", "files/docs%2Fa.png", b"/api/v1/files/docs%2Fa.png"),
        ("non_ascii", "files/\u4e2d\u6587.txt", b"/api/v1/files/%E4%B8%AD%E6%96%87.txt"),
        ("plus", "files/a+b.txt", b"/api/v1/files/a+b.txt"),
        ("ampersand", "files/a&b.txt", b"/api/v1/files/a&b.txt"),
    ],
)
def test_build_upstream_url_encodes_path_component_delimiters(
    label: str,
    path: str,
    expected_raw_path: bytes,
) -> None:
    """路由参数里的 #/? 必须编码后转发，而不是让 httpx 抛 InvalidURL。

    uvicorn 只对请求行解码一层，因此 ``/api/v1/files/a%23b.txt`` 到达代理时
    ``path`` 参数是 ``files/a#b.txt``。原实现把它直接交给 ``httpx.URL.copy_with``，
    httpx 按 RFC 3986 判定 # 是 fragment 起始、? 是 query 起始，两者都不能出现在
    path 组件里，于是抛 ``InvalidURL``；TraceMiddleware 把它转成 500，前端对这类
    文件名（例如 ``a#b.txt``、``a?b.txt``）看到的是「边界输入泄漏成 500」。

    修复只针对 path 组件里会被误解为分隔符的字符做百分号编码；已编码的 %XX、
    编码斜杠、非 ASCII 与其它已合法字符的线上字节必须完全不变。
    """

    url = build_upstream_url(
        "http://127.0.0.1:41001",
        ("api", "v1"),
        path,
    )

    assert url.raw_path == expected_raw_path
