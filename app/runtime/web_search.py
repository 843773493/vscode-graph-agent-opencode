"""按调用绑定搜索 SDK 的 HTTP 客户端，SDK 依赖仅在实际搜索时加载。"""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import ExitStack

import httpx

from app.runtime.network_proxy import ProxyRoutes


class _SearchHTTPClient(httpx.Client):
    @property
    def client(self) -> _SearchHTTPClient:
        return self

    def set_cookies(self, url: str, cookies: Mapping[str, str] | None) -> None:
        host = httpx.URL(url if "://" in url else "https://" + url).host
        for name, value in (cookies or {}).items():
            self.cookies.set(name, value, domain=host)


def search_web(
    query: str, *, routes: ProxyRoutes, max_results: int, search_type: str,
    region: str, safesearch: str, time_range: str | None,
) -> list[dict[str, str]]:
    from ddgs.ddgs import DDGS
    from ddgs.engines import ENGINES
    from ddgs.engines.duckduckgo import Duckduckgo

    # TODO: ddgs 9.14.4 只有单代理参数且会读取全局环境；SDK 提供实例 client factory 后改用公开注入点。
    # 原引擎只依赖 request 的 status_code/content/text 和 client.set_cookies；HTTPX 保留这些真实接口。
    class WorkspaceSearch(DDGS):
        def _get_network_client(self) -> None:
            # 不采用 SDK 跨实例的可选远程结果缓存，避免借用其他 owner 的网络客户端。
            return None

    verify = httpx.create_ssl_context(trust_env=True)
    with ExitStack() as stack:
        search = WorkspaceSearch()
        for engine_type in set(ENGINES[search_type].values()):
            duckduckgo_text = issubclass(engine_type, Duckduckgo)
            headers = engine_type.headers if duckduckgo_text else engine_type.headers_update
            client = stack.enter_context(_SearchHTTPClient(
                trust_env=False, verify=verify, timeout=search._timeout,
                follow_redirects=not duckduckgo_text, headers=headers,
                mounts=routes.sync_mounts(verify=verify),
            ))

            class BoundEngine(engine_type):
                def __init__(self, http_client: _SearchHTTPClient) -> None:
                    self.http_client = http_client
                    self.results = []

            search._engines_cache[engine_type] = BoundEngine(client)
        results = (search.text if search_type == "text" else search.news)(
            query, region=region, safesearch=safesearch, timelimit=time_range, max_results=max_results,
        )
    return [{"title": result["title"], "url": result["href" if search_type == "text" else "url"],
             "snippet": result["body"],
             **({"date": result["date"], "source": result["source"]} if search_type == "news" else {})}
            for result in results]
