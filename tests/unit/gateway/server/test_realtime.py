"""统一 WS 真实经过 ASGI owner、鉴权和 Trace；不启动后台服务。"""
from __future__ import annotations

import asyncio
import json
import struct
import threading

import pytest
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.testclient import TestClient

from app.core.shielded_cleanup import run_cleanup_shielded
from app.core.trace_middleware import TraceMiddleware
from app.gateway.proxy_upstream import ProxyStreamingResponse
from app.gateway.server import realtime as api


@pytest.fixture
def realtime_client(monkeypatch):
    monkeypatch.setattr(api, "verify_gateway_token", lambda token: token if token == "test-token" else _unauthorized())
    app = FastAPI()
    app.add_middleware(TraceMiddleware)
    app.include_router(api.router)
    calls, closed = [], []
    cleanup_started, cleanup_release = threading.Event(), threading.Event()
    app.state.realtime_cleanup = (cleanup_started, cleanup_release)

    @app.get('/api/v1/session-catalog/events/stream')
    async def events(request: Request, after: int = 0, x_boxteam_workspace_id: str | None = Header(default=None)):
        calls.append((after, x_boxteam_workspace_id, request.state.request_id))
        if after == 410:
            raise HTTPException(410, detail={"code": "cursor_gone"})

        async def generate():
            try:
                if after == 500:
                    raise RuntimeError("owner-failed")
                if after == 999:
                    yield b'x' * (2 * 1024 * 1024)
                else:
                    yield f'id: {after + 1}\nevent: changed\ndata: {{}}\n\n'.encode()
                await asyncio.Event().wait()
            finally:
                if after == 777:
                    cleanup_started.set()
                    async def finish():
                        await asyncio.to_thread(cleanup_release.wait)
                        closed.append(after)
                    await run_cleanup_shielded(finish)
                else:
                    closed.append(after)
        return ProxyStreamingResponse(generate(), media_type='text/event-stream')

    with TestClient(app) as client:
        yield client, calls, closed


def _unauthorized():
    raise HTTPException(401, 'invalid token')


def authenticate(client):
    socket = client.websocket_connect('/api/gateway/realtime')
    return socket


def subscribe(socket, identity, after=0, workspace='one'):
    socket.send_json({"type": "subscribe", "subscription_id": identity,
                      "path": f'/api/v1/session-catalog/events/stream?after={after}',
                      "headers": {"X-BoxTeam-Workspace-ID": workspace}})


def test_multiple_workspaces_and_cancel_keep_other_subscription(realtime_client):
    client, calls, closed = realtime_client
    with authenticate(client) as socket:
        socket.send_json({"type": "auth", "token": "test-token"})
        ready = socket.receive_json()
        assert ready['type'] == 'ready' and ready['request_id']
        subscribe(socket, 1, 7)
        header = socket.receive_json()
        assert header['type'] == 'headers' and header['status'] == 200
        assert header['headers']['x-request-id'] == calls[0][2] != ready['request_id']
        assert socket.receive_bytes() == struct.pack('!I', 1) + b'id: 8\nevent: changed\ndata: {}\n\n'
        subscribe(socket, 2, 12, 'two')
        assert socket.receive_json()['subscription_id'] == 2
        assert struct.unpack('!I', socket.receive_bytes()[:4]) == (2,)
        socket.send_json({'type': 'unsubscribe', 'subscription_id': 1})
        subscribe(socket, 3, 20)
        assert socket.receive_json()['subscription_id'] == 3
        socket.receive_bytes()
        assert 12 not in closed
        assert [(call[0], call[1]) for call in calls] == [(7, 'one'), (12, 'two'), (20, 'one')]
    assert set(closed) == {7, 12, 20}


def test_http_failure_preserves_status_and_other_subscription(realtime_client):
    client, _, _ = realtime_client
    with authenticate(client) as socket:
        socket.send_json({'type': 'auth', 'token': 'test-token'})
        socket.receive_json()
        subscribe(socket, 1, 410)
        response = socket.receive_json()
        assert response['status'] == 410 and response['headers']['x-request-id']
        assert b'cursor_gone' in socket.receive_bytes()
        assert socket.receive_json() == {'type': 'end', 'subscription_id': 1}
        subscribe(socket, 2, 2)
        assert socket.receive_json()['status'] == 200
        socket.receive_bytes()


def test_slow_large_subscription_does_not_block_control_or_other_stream(realtime_client):
    client, _, closed = realtime_client
    with authenticate(client) as socket:
        socket.send_json({'type': 'auth', 'token': 'test-token'})
        socket.receive_json()
        subscribe(socket, 1, 999)
        assert socket.receive_json()['status'] == 200
        sent = 0
        while sent < api._WINDOW_BYTES:
            sent += len(socket.receive_bytes()) - 4
        assert sent == api._WINDOW_BYTES
        # 不确认大流；它必须独立反压，另一流仍能返回。
        subscribe(socket, 2, 3)
        assert socket.receive_json()['subscription_id'] == 2
        assert struct.unpack('!I', socket.receive_bytes()[:4]) == (2,)
        socket.send_json({'type': 'ack', 'subscription_id': 1, 'consumed_bytes': sent})
        assert struct.unpack('!I', socket.receive_bytes()[:4]) == (1,)
        socket.send_json({'type': 'unsubscribe', 'subscription_id': 1})
        # 后续订阅仍可建立；连接关闭时必须等全部取消任务结清。
        subscribe(socket, 3, 4)
        # 取消前已发送的数据仍可能在途；只能跳过受字节窗口约束的原订阅数据。
        in_flight = 0
        for _ in range(api._WINDOW_BYTES // api._CHUNK_BYTES + 1):
            message = socket.receive()
            if 'text' in message:
                assert json.loads(message['text'])['subscription_id'] == 3
                break
            chunk = message['bytes']
            assert struct.unpack('!I', chunk[:4]) == (1,)
            in_flight += len(chunk) - 4
            assert in_flight <= api._WINDOW_BYTES
        else:
            pytest.fail('取消大流后，后续订阅没有返回响应头')
        assert struct.unpack('!I', socket.receive_bytes()[:4]) == (3,)
    assert set(closed) == {999, 3, 4}


def test_slow_unsubscribe_cleanup_keeps_other_control_frames_live(realtime_client):
    client, _, closed = realtime_client
    started, release = client.app.state.realtime_cleanup
    # 旧实现会等到保险释放才处理下一帧；新实现须在释放之前返回兄弟订阅。
    safeguard = threading.Timer(2, release.set)
    try:
        with authenticate(client) as socket:
            socket.send_json({'type': 'auth', 'token': 'test-token'})
            socket.receive_json()
            subscribe(socket, 1, 777)
            assert socket.receive_json()['subscription_id'] == 1
            socket.receive_bytes()
            safeguard.start()
            socket.send_json({'type': 'unsubscribe', 'subscription_id': 1})
            subscribe(socket, 2, 8)
            assert socket.receive_json()['subscription_id'] == 2
            assert struct.unpack('!I', socket.receive_bytes()[:4]) == (2,)
            assert started.wait(1) and not release.is_set()
            assert 777 not in closed
            release.set()
        assert set(closed) == {777, 8}
    finally:
        release.set()
        safeguard.cancel()


def test_rejects_invalid_auth_and_unregistered_write(realtime_client):
    client, calls, _ = realtime_client
    with authenticate(client) as socket:
        socket.send_json({'type': 'auth', 'token': 'wrong'})
        assert socket.receive_json()['status'] == 401
    with authenticate(client) as socket:
        socket.send_json({'type': 'auth', 'token': 'test-token'})
        socket.receive_json()
        socket.send_json({'type': 'subscribe', 'subscription_id': 1, 'path': '/api/v1/sessions', 'method': 'POST'})
        error = socket.receive_json()
        assert error['type'] == 'error' and error['subscription_id'] == 1
        assert calls == []
