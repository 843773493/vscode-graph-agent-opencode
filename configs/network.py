"""两个配置域复用的代理互斥字段；凭据值由原执行边界准备。"""

from __future__ import annotations

from typing import Annotated, Literal

import httpx
from httpx._utils import URLPattern, is_ipv4_hostname, is_ipv6_hostname
from pydantic import BaseModel, ConfigDict, Field, field_validator


def proxy_bypass_patterns(hosts: tuple[str, ...]) -> tuple[str, ...]:
    """沿 HTTPX 的主机匹配语义准备 mounts，不实现另一套匹配器。"""
    patterns = []
    for hostname in hosts:
        if not hostname:
            continue
        if hostname == "*":
            return ("all://",)
        if "://" in hostname:
            pattern = hostname
        elif is_ipv4_hostname(hostname) or hostname.casefold() == "localhost":
            pattern = f"all://{hostname}"
        elif is_ipv6_hostname(hostname):
            pattern = f"all://[{hostname}]"
        else:
            pattern = f"all://*{hostname}"
        # TODO: HTTPX 升级时复验该内部纯 matcher 入口；不在请求中增加版本探测。
        URLPattern(pattern)
        patterns.append(pattern)
    return tuple(patterns)


class ProxyAuthentication(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    username: str = Field(min_length=1)
    password: str = Field(pattern=r"^secret:sec_[0-9a-f]{32}$", repr=False)


class EnvironmentProxy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    mode: Literal["environment"] = "environment"


class DirectProxy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    mode: Literal["direct"] = "direct"


class ExplicitProxy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    mode: Literal["explicit"] = "explicit"
    url: str
    bypass: tuple[str, ...] = ()
    auth: ProxyAuthentication | None = None

    @field_validator("url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        try:
            url = httpx.URL(value)
        except httpx.InvalidURL as error:
            raise ValueError("代理 URL 语法无效") from error
        if url.scheme not in {"http", "https"} or not url.host:
            raise ValueError("代理 URL 必须为带主机的 HTTP／HTTPS 地址")
        if url.userinfo or url.query or url.fragment or url.path not in {"", "/"}:
            raise ValueError("代理 URL 不接受认证、查询、fragment 或业务路径")
        return value

    @field_validator("bypass")
    @classmethod
    def validate_bypass(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        # HTTPX 按主机／端口匹配；CIDR 和 PAC 不属于其已支持合同。
        try:
            for host in value:
                if not host or host != host.strip() or ("/" in host and "://" not in host):
                    raise ValueError("代理绕过项必须为非空主机／端口或受支持的 URL pattern")
                for normalized in proxy_bypass_patterns((host,)):
                    pattern = httpx.URL(normalized)
                    if pattern.scheme not in {"http", "https", "all"} or pattern.userinfo or pattern.path not in {"", "/"} or pattern.query or pattern.fragment:
                        raise ValueError("代理绕过项不接受未知 scheme、认证、路径、查询或 fragment")
        except httpx.InvalidURL as error:
            raise ValueError("代理绕过项语法无效") from error
        return value


ProxyConfiguration = Annotated[EnvironmentProxy | DirectProxy | ExplicitProxy, Field(discriminator="mode")]


class NetworkSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    proxy: ProxyConfiguration = Field(json_schema_extra={"x-merge": "replace"})
