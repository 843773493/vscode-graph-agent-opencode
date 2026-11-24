"""捕获环境并准备 HTTPX 路由；连接和关闭仍由原客户端 owner 持有。"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from ssl import SSLContext
from types import MappingProxyType

import httpx

from configs.network import (
    DirectProxy,
    EnvironmentProxy,
    ExplicitProxy,
    proxy_bypass_patterns,
)


def _proxy_environment(environment: Mapping[str, str]) -> dict[str, str]:
    """复用 urllib 的大小写／CGI 规则，但只接受传入快照，不读取系统设置。"""
    proxies = {}
    for key, value in environment.items():
        lowered = key.casefold()
        if value and lowered in {"http_proxy", "https_proxy", "all_proxy", "no_proxy"}:
            proxies[lowered.removesuffix("_proxy")] = value
    if "REQUEST_METHOD" in environment:
        proxies.pop("http", None)
    for key in ("http_proxy", "https_proxy", "all_proxy", "no_proxy"):
        if key in environment:
            if environment[key]:
                proxies[key.removesuffix("_proxy")] = environment[key]
            else:
                proxies.pop(key.removesuffix("_proxy"), None)
    return proxies


def capture_proxy_environment() -> Mapping[str, str]:
    """owner 准备配置时捕获标准代理变量，后续请求不重读进程或系统设置。"""
    return MappingProxyType({key: value for key, value in os.environ.items()
        if key.casefold() in {"http_proxy", "https_proxy", "all_proxy", "no_proxy"} or key == "REQUEST_METHOD"})


@dataclass(frozen=True, slots=True)
class ProxyRoutes:
    # HTTPX Proxy 的 repr 会隐藏密码；整组路由也不进入自动 dataclass repr。
    routes: tuple[tuple[str, httpx.Proxy | None], ...] = field(repr=False)

    @property
    def revision(self) -> str:
        """驻留客户端身份只使用摘要，不把代理认证放入代键或日志。"""
        values = [(pattern, str(proxy.url) if proxy is not None else None,
                   [part.decode() for part in proxy.raw_auth] if proxy is not None and proxy.raw_auth else None)
                  for pattern, proxy in self.routes]
        return hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()

    @classmethod
    def from_configuration(
        cls,
        configuration: EnvironmentProxy | DirectProxy | ExplicitProxy,
        *,
        environment: Mapping[str, str] | None = None,
        password: str | None = None,
    ) -> ProxyRoutes:
        if isinstance(configuration, DirectProxy):
            return cls(())
        routes: dict[str, httpx.Proxy | None] = {}
        if isinstance(configuration, EnvironmentProxy):
            proxies = _proxy_environment(dict(os.environ) if environment is None else environment)
            for scheme in ("http", "https", "all"):
                value = proxies.get(scheme)
                if value:
                    url = value if "://" in value else "http://" + value
                    if httpx.URL(url).scheme not in {"http", "https"}:
                        raise ValueError("环境代理类型未支持；需要 HTTP／HTTPS 代理")
                    routes[f"{scheme}://"] = httpx.Proxy(url)
            bypass = tuple(host.strip() for host in proxies.get("no", "").split(","))
        else:
            authentication = configuration.auth
            auth = None
            if authentication is not None:
                if password is None:
                    raise ValueError("代理所需命名密钥未准备")
                auth = (authentication.username, password)
            routes["all://"] = httpx.Proxy(configuration.url, auth=auth)
            bypass = configuration.bypass
        patterns = proxy_bypass_patterns(bypass)
        if patterns == ("all://",):
            return cls(())
        routes.update((pattern, None) for pattern in patterns)
        return cls(tuple(routes.items()))

    def async_mounts(self, *, verify: SSLContext) -> dict[str, httpx.AsyncBaseTransport | None]:
        return {pattern: None if proxy is None else httpx.AsyncHTTPTransport(proxy=proxy, verify=verify, trust_env=False)
                for pattern, proxy in self.routes}

    def sync_mounts(self, *, verify: SSLContext) -> dict[str, httpx.BaseTransport | None]:
        return {pattern: None if proxy is None else httpx.HTTPTransport(proxy=proxy, verify=verify, trust_env=False)
                for pattern, proxy in self.routes}
