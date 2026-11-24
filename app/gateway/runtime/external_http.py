"""Gateway 外部 HTTP 的代理代次；旧响应体结清后才释放旧连接池。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass

import httpx
from boxteam_plugin_sdk.lifecycle import run_cleanup_shielded

from app.core.shielded_cleanup import run_sync_shielded
from app.gateway.runtime.consumer_protocol import (
    GatewayRuntimeConsumerStage,
    GatewayRuntimeHealthProof,
)
from app.gateway.secrets.vault import SecretVault
from app.runtime.network_proxy import ProxyRoutes
from configs.network import ExplicitProxy, NetworkSettings


@dataclass(eq=False)
class _Pool:
    client: httpx.AsyncClient
    settings: NetworkSettings
    routes: ProxyRoutes
    secret_revision: int | None
    readers: int = 0


class _ResponseStream(httpx.AsyncByteStream):
    def __init__(self, response: httpx.Response, owner: GatewayExternalHTTPTransport, pool: _Pool) -> None:
        self.response, self.owner, self.pool = response, owner, pool
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        async for chunk in self.response.aiter_raw():
            yield chunk

    async def aclose(self) -> None:
        if self.closed:
            return
        self.closed = True

        async def close() -> None:
            try:
                await self.response.aclose()
            finally:
                self.pool.readers -= 1
                await self.owner._close_retired(self.pool)

        await run_cleanup_shielded(close)


class GatewayExternalHTTPTransport(httpx.AsyncBaseTransport):
    def __init__(self, vault: SecretVault) -> None:
        self.vault = vault
        self._active: _Pool | None = None
        self._pools: set[_Pool] = set()
        self._secret_changed = 0
        self._closed = False
        self._switch_lock = asyncio.Lock()

    async def prepare(self, payload: object) -> _Pool:
        if self._closed:
            raise RuntimeError("Gateway 外部 HTTP owner 已关闭")
        settings = NetworkSettings.model_validate(payload)
        password = None
        revision = None
        if isinstance(settings.proxy, ExplicitProxy) and settings.proxy.auth is not None:
            secret_id = settings.proxy.auth.password.removeprefix("secret:")
            secrets = await run_sync_shielded(self.vault.resolve, [secret_id])
            secret = secrets[secret_id]
            password, revision = secret.value.get_secret_value(), secret.revision
        routes = ProxyRoutes.from_configuration(settings.proxy, password=password)
        for pool in self._pools:
            if (not pool.client.is_closed and pool.settings == settings and pool.secret_revision == revision
                    and _route_key(pool.routes) == _route_key(routes)):
                return pool
        # trust_env 仅用于保留 HTTPX 的 SSL_CERT_FILE／SSL_CERT_DIR；代理已显式捕获。
        verify = httpx.create_ssl_context(trust_env=True)
        pool = _Pool(httpx.AsyncClient(trust_env=False, verify=verify, timeout=None,
                                      mounts=routes.async_mounts(verify=verify)), settings, routes, revision)
        self._pools.add(pool)
        return pool

    @asynccontextmanager
    async def candidate_client(self, payload: object) -> AsyncIterator[httpx.AsyncClient]:
        """候选消费者显式借用候选出口，不经当前 active 或其密钥刷新锁。"""
        pool = await self.prepare(payload)
        pool.readers += 1
        try:
            yield pool.client
        finally:
            async def release() -> None:
                pool.readers -= 1
                await self._close_retired(pool)

            await run_cleanup_shielded(release)

    async def start(self, payload: object) -> None:
        if self._active is not None:
            raise RuntimeError("Gateway 外部 HTTP owner 已启动")
        self._active = await self.prepare(payload)

    def invalidate_secret(self, secret_id: str) -> None:
        for pool in self._pools:
            if not isinstance(pool.settings.proxy, ExplicitProxy):
                continue
            auth = pool.settings.proxy.auth
            if auth is not None and auth.password == f"secret:{secret_id}":
                self._secret_changed += 1
                return

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if self._closed or self._active is None:
            raise RuntimeError("Gateway 外部 HTTP owner 未就绪")
        if self._secret_changed:
            async with self._switch_lock:
                if self._secret_changed:
                    change = self._secret_changed
                    previous = self._active
                    replacement = await self.prepare(previous.settings)
                    self._active = replacement
                    if self._secret_changed == change:
                        self._secret_changed = 0
                    await self._close_retired(previous)
        pool = self._active
        pool.readers += 1
        try:
            response = await pool.client.send(request, stream=True)
        except BaseException:
            pool.readers -= 1
            await self._close_retired(pool)
            raise
        return httpx.Response(response.status_code, headers=response.headers, request=request,
                              extensions=response.extensions, stream=_ResponseStream(response, self, pool))

    async def _close_retired(self, pool: _Pool) -> None:
        if pool is not self._active and pool.readers == 0 and pool in self._pools:
            await pool.client.aclose()
            self._pools.discard(pool)

    def stage(self, *, payload: object, generation: str, fencing_token_digest: str | None,
              fence_check: Callable[[], None] | None) -> GatewayRuntimeConsumerStage:
        previous: _Pool | None = None
        candidate: _Pool | None = None
        locked = False

        async def prepare() -> None:
            nonlocal candidate, previous, locked
            await self._switch_lock.acquire()
            locked = True
            previous = self._active
            # apply 后仍可能回退；原池在本次事务结清前不能被响应关闭回调回收。
            if previous is not None:
                previous.readers += 1
            candidate = await self.prepare(payload)
            candidate.readers += 1

        def apply() -> None:
            if candidate is None:
                raise RuntimeError("Gateway 外部 HTTP 候选尚未准备")
            self._active = candidate

        def health() -> GatewayRuntimeHealthProof:
            if candidate is None or self._active is not candidate:
                raise RuntimeError("Gateway 外部 HTTP 代理代次不匹配")
            return GatewayRuntimeHealthProof("gateway-external-http", generation, "healthy",
                {"mode": candidate.settings.proxy.mode, "secret_revision": candidate.secret_revision}, fencing_token_digest)

        def rollback() -> None:
            if self._active is candidate:
                self._active = previous

        async def finalize() -> None:
            errors: list[Exception] = []
            try:
                for pool in (previous, candidate):
                    if pool is not None:
                        pool.readers -= 1
                for pool in {previous, candidate} - {None}:
                    try:
                        await self._close_retired(pool)
                    except Exception as error:  # noqa: BLE001 — 所有池都必须完成引用结算。
                        errors.append(error)
            finally:
                if locked:
                    self._switch_lock.release()
            if errors:
                raise ExceptionGroup("Gateway 外部 HTTP 连接池释放失败", errors)

        return GatewayRuntimeConsumerStage("gateway-external-http", generation, prepare, apply, health,
            lambda: None, rollback, fencing_token_digest, fence_check, finalize)

    async def aclose(self) -> None:
        self._closed = True
        for pool in tuple(self._pools):
            await pool.client.aclose()
            self._pools.discard(pool)


def _route_key(routes: ProxyRoutes) -> tuple[object, ...]:
    # 只在准备边界比较有效出口，不以 Proxy 实例身份导致相同策略重复建池。
    return tuple((pattern, None if proxy is None else (proxy.url, proxy.auth, tuple(proxy.headers.multi_items())))
                 for pattern, proxy in routes.routes)
