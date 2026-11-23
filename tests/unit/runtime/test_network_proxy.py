"""环境来源只读、模式互斥及 HTTPX 路由语义。"""

from __future__ import annotations

import urllib.request

import httpx
import pytest
from pydantic import ValidationError

from app.runtime.network_proxy import ProxyRoutes
from configs.network import (
    DirectProxy,
    EnvironmentProxy,
    ExplicitProxy,
    NetworkSettings,
)


@pytest.mark.parametrize("proxy", [
    {"mode": "direct", "url": "http://proxy.invalid"},
    {"mode": "environment", "bypass": []},
    {"mode": "explicit"},
    {"mode": "explicit", "url": "socks5://proxy.invalid"},
    {"mode": "explicit", "url": "http://name:password@proxy.invalid"},
    {"mode": "explicit", "url": "http://proxy.invalid/path"},
    {"mode": "explicit", "url": "http://proxy.invalid:not-a-port"},
    {"mode": "explicit", "url": "http://proxy.invalid", "bypass": ["192.168.0.0/16"]},
    {"mode": "explicit", "url": "http://proxy.invalid", "bypass": ["example.org?query=ignored"]},
    {"mode": "explicit", "url": "http://proxy.invalid", "bypass": ["user@host"]},
    {"mode": "explicit", "url": "http://proxy.invalid", "bypass": ["*", "host:invalid-port"]},
    {"mode": "explicit", "url": "http://proxy.invalid", "auth": {"username": "name", "password": "raw-password"}},
])
def test_invalid_mode_blocks_fail_at_configuration_boundary(proxy: dict):
    with pytest.raises(ValidationError):
        NetworkSettings.model_validate({"proxy": proxy})


def test_environment_reads_only_the_supplied_snapshot(monkeypatch: pytest.MonkeyPatch):
    def unexpected_system_lookup():
        raise AssertionError("不得读取注册表／系统代理")

    monkeypatch.setattr(urllib.request, "getproxies", unexpected_system_lookup)
    monkeypatch.setenv("HTTPS_PROXY", "http://unrelated.invalid:1234")
    assert ProxyRoutes.from_configuration(EnvironmentProxy(), environment={}).routes == ()
    environment = {"HTTPS_PROXY": "http://upper.invalid", "https_proxy": "http://lower.invalid"}
    routes = dict(ProxyRoutes.from_configuration(EnvironmentProxy(), environment=environment).routes)
    assert str(routes["https://"].url) == "http://lower.invalid"
    environment["https_proxy"] = "http://changed.invalid"
    assert str(routes["https://"].url) == "http://lower.invalid"
    assert dict(ProxyRoutes.from_configuration(EnvironmentProxy(), environment={
        "HTTPS_PROXY": "http://upper.invalid", "https_proxy": "",
    }).routes) == {}


def test_cgi_http_proxy_protection_and_lowercase_opt_in():
    assert ProxyRoutes.from_configuration(EnvironmentProxy(), environment={
        "REQUEST_METHOD": "GET", "HTTP_PROXY": "http://untrusted.invalid",
    }).routes == ()
    routes = dict(ProxyRoutes.from_configuration(EnvironmentProxy(), environment={
        "REQUEST_METHOD": "GET", "HTTP_PROXY": "http://untrusted.invalid", "http_proxy": "http://chosen.invalid",
    }).routes)
    assert str(routes["http://"].url) == "http://chosen.invalid"


def test_explicit_and_direct_do_not_mix_environment_or_bypass():
    environment = {"HTTP_PROXY": "http://wrong.invalid", "NO_PROXY": "*"}
    explicit = ProxyRoutes.from_configuration(ExplicitProxy(url="http://chosen.invalid"), environment=environment)
    assert str(dict(explicit.routes)["all://"].url) == "http://chosen.invalid"
    assert ProxyRoutes.from_configuration(DirectProxy(), environment=environment).routes == ()
    assert ProxyRoutes.from_configuration(EnvironmentProxy(), environment=environment).routes == ()


def test_no_proxy_uses_httpx_host_and_ipv6_patterns():
    routes = dict(ProxyRoutes.from_configuration(EnvironmentProxy(), environment={
        "ALL_PROXY": "http://proxy.invalid", "NO_PROXY": "localhost,::1,example.org,.sub.example.org",
    }).routes)
    assert routes["all://localhost"] is None
    assert routes["all://[::1]"] is None
    assert routes["all://*example.org"] is None
    assert routes["all://*.sub.example.org"] is None


def test_explicit_auth_requires_prepared_secret_and_redacts_repr():
    configuration = ExplicitProxy.model_validate({
        "url": "http://proxy.invalid",
        "auth": {"username": "name", "password": "secret:sec_0123456789abcdef0123456789abcdef"},
    })
    with pytest.raises(ValueError, match="未准备"):
        ProxyRoutes.from_configuration(configuration)
    routes = ProxyRoutes.from_configuration(configuration, password="secret-value")
    assert dict(routes.routes)["all://"].auth == ("name", "secret-value")
    assert "secret-value" not in repr(routes)
    assert "secret-value" not in repr(dict(routes.routes))


def test_environment_proxy_credentials_are_not_in_public_repr():
    routes = ProxyRoutes.from_configuration(EnvironmentProxy(), environment={
        "HTTP_PROXY": "http://name:secret-value@proxy.invalid",
    })
    assert "secret-value" not in repr(routes)
    assert dict(routes.routes)["http://"].auth == ("name", "secret-value")


def test_routes_choose_bypass_before_catchall_without_environment_leak():
    routes = ProxyRoutes.from_configuration(ExplicitProxy(url="http://proxy.invalid", bypass=("localhost",)))
    with httpx.Client(trust_env=False, mounts=routes.sync_mounts(verify=httpx.create_ssl_context(trust_env=False))) as client:
        assert client._transport_for_url(httpx.URL("http://localhost")) is client._transport
        assert client._transport_for_url(httpx.URL("http://external.invalid")) is not client._transport
