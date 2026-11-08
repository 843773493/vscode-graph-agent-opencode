"""真实 Gateway 精确服务流：帧保真、断流、旧实例及空闲 SSE 退役。"""

from __future__ import annotations

import concurrent.futures
import gzip
import hashlib
import json
import zipfile
from pathlib import Path

import commentjson
import httpx
import pytest
from websockets.exceptions import ConnectionClosed, InvalidStatus
from websockets.sync.client import connect

from app.schemas.gateway.service_plugin import PluginSlotStatusDTO
from tests.harness.python.run_context import TestRunContext
from tests.integration.gateway.test_service_plugins import (
    plugin_gateway as plugin_gateway,  # noqa: PLC0414 — pytest 复用原独立 Gateway fixture。
)

PLUGIN = '''
import gzip, json, os
from fastapi import FastAPI, Request, WebSocket
from fastapi.responses import Response, StreamingResponse
import asyncio, uvicorn
identity = json.loads(os.environ['BOXTEAM_PLUGIN_INSTANCE'])
identity.update(status='ready', process_id=os.getpid(), capabilities=['status','shutdown'],
    log_sink=json.loads(os.environ['BOXTEAM_RUNTIME_LOG_SINK']))
app = FastAPI()
@app.middleware('http')
async def token(request, call_next):
    if request.headers.get('X-Local-Token') != os.environ['BOXTEAM_RUNTIME_CONTROL_TOKEN']:
        from fastapi.responses import Response
        return Response(status_code=403)
    return await call_next(request)
@app.get('/internal/plugin/status')
async def status(): return identity
@app.post('/internal/plugin/shutdown')
async def shutdown():
    server.should_exit = True
    return {}
@app.get('/services/events/stream')
async def events(request: Request):
    async def data():
        yield ('data: '+json.dumps({'instance_id':identity['instance_id'],
            'request_id':request.headers.get('X-Request-ID')})+'\\n\\n').encode()
        await asyncio.Event().wait()
    return StreamingResponse(data(), media_type='text/event-stream')
@app.post('/services/events/compressed')
async def compressed(request: Request):
    body = await request.body()
    if request.headers.get('content-encoding') == 'gzip': body = gzip.decompress(body)
    return Response(gzip.compress(body), media_type='application/octet-stream', headers={
        'content-encoding':'gzip', 'X-Local-Token':request.headers.get('X-Local-Token',''),
        'X-BoxTeam-Federation-Token':'must-not-leak', 'X-BoxTeam-Target-Gateway-Id':'must-not-leak',
        'Set-Cookie':'must-not-leak'})
@app.websocket('/services/socket/echo')
async def socket(websocket: WebSocket):
    if websocket.headers.get('X-Local-Token') != os.environ['BOXTEAM_RUNTIME_CONTROL_TOKEN']:
        await websocket.close(code=1008); return
    await websocket.accept()
    await websocket.send_json({'instance_id':identity['instance_id'],
        'request_id':websocket.headers.get('X-Request-ID'),
        'query':websocket.url.query})
    while True:
        message=await websocket.receive()
        if message['type']=='websocket.disconnect': return
        if message.get('text') is not None: await websocket.send_text(message['text'])
        else: await websocket.send_bytes(message['bytes'])
server=uvicorn.Server(uvicorn.Config(app,host='127.0.0.1',port=int(os.environ['BOXTEAM_PLUGIN_PORT']),
    log_level='warning',timeout_graceful_shutdown=1))
server.run()
'''


def install_stream_plugin(client: httpx.Client, context: TestRunContext) -> PluginSlotStatusDTO:
    gateway_id = client.get("/api/gateway/health").raise_for_status().json()["data"]["gateway_id"]
    manifest = {"manifest_version": 1, "package_id": "stream.probe", "version": "1.0.0",
        "name": "流协议探针", "extension_contract_version": 1, "plugins": [{
            "id": "streams", "name": "服务流", "instance_scope": "gateway", "runtime": "python",
            "entrypoint": ["server.py"], "management_protocol_version": 1,
            "requires": [{"extension_point": "services", "version": 1}],
            "contributes": {"services": [
                {"service_id": "events", "service_type": "terminal", "protocol": "http"},
                {"service_id": "socket", "service_type": "terminal", "protocol": "websocket"}]}}]}
    archive_path: Path = context.artifacts_dir / "stream-probe.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("plugin.jsonc", json.dumps(manifest))
        archive.writestr("server.py", PLUGIN)
    client.post("/api/gateway/service-plugins/install", json={"gateway_id": gateway_id,
        "path": str(archive_path), "expected_digest": hashlib.sha256(archive_path.read_bytes()).hexdigest()}).raise_for_status()
    source_path = "/api/gateway/config/sources/gateway_mutable_override"
    source = client.get(source_path).raise_for_status().json()["data"]
    document = commentjson.loads(source["jsonc"])
    document.setdefault("plugins", {})["runtime"] = {"entries": [{"package_id": "stream.probe",
        "plugin_id": "streams", "package_version": "1.0.0", "enabled": True}]}
    client.put(source_path, json={"gateway_id": gateway_id, "source_key": "gateway_mutable_override",
        "jsonc": json.dumps(document), "base_file_digest": source["file_digest"],
        "base_layer_revision": source["layer_revision"], "base_layer_digest": source["layer_digest"],
        "expected_active_revision": source["active_revision"], "expected_active_digest": source["active_digest"],
        "idempotency_key": "enable-stream-probe"}).raise_for_status()
    slot = PluginSlotStatusDTO.model_validate(client.post("/api/gateway/service-plugins/status",
        json={"gateway_id": gateway_id}).raise_for_status().json()["data"]["plugins"][0])
    assert slot.state == "ready" and slot.instance is not None, slot
    return slot


def test_service_streams_remain_bound_and_retire_with_original_instance(
    plugin_gateway: tuple[httpx.Client, TestRunContext],
) -> None:
    client, context = plugin_gateway
    slot = install_stream_plugin(client, context)
    assert slot.instance is not None
    gateway_id = slot.gateway_id
    instance_id = slot.instance.instance_id
    base = f"/api/gateway/service-plugins/stream.probe/streams/instances/{instance_id}/services"
    payload = '压缩正文🙂'.encode()
    compressed = client.post(base + '/events/compressed?package_version=1.0.0', content=gzip.compress(payload),
        headers={'Content-Encoding': 'gzip', 'X-Request-ID': 'plugin-compression-original'}).raise_for_status()
    assert compressed.content == payload
    assert compressed.headers['X-Request-ID'] == 'plugin-compression-original'
    assert not {'x-local-token', 'x-boxteam-federation-token', 'x-boxteam-target-gateway-id',
        'set-cookie', 'content-encoding'} & set(compressed.headers)
    socket_url = str(client.base_url).replace("http:", "ws:").rstrip("/") + base + "/socket/echo?package_version=1.0.0&marker=original"
    evidence: dict[str, object] = {"original_instance": instance_id}
    socket_headers = {"X-Local-Token": client.headers["X-Local-Token"]}
    with connect(socket_url, additional_headers={**socket_headers, "X-Request-ID": "plugin-socket-original"},
        open_timeout=10, close_timeout=3) as socket:
        receipt = json.loads(socket.recv(timeout=5))
        evidence["websocket"] = receipt
        assert receipt["request_id"] == "plugin-socket-original"
        assert receipt["instance_id"] == instance_id
        assert receipt["query"] == "marker=original", "管理字段和凭据不能成为业务查询"
        for message in ("第一帧🙂", b"\x00\xff\x03", "第三帧"):
            socket.send(message)
            assert socket.recv(timeout=5) == message
        # HTTP 不得借同名 WebSocket 服务访问未声明传输。
        assert client.get(base + "/socket/echo?package_version=1.0.0").status_code == 409
        with client.stream("GET", base + "/events/stream?package_version=1.0.0",
            headers={"X-Request-ID": "plugin-sse-original"}, timeout=15) as response:
            response.raise_for_status()
            lines = response.iter_lines()
            event = json.loads(next(lines).removeprefix("data: "))
            assert event == {"instance_id": instance_id, "request_id": "plugin-sse-original"}
            assert next(lines) == ""

            def restart() -> PluginSlotStatusDTO:
                return PluginSlotStatusDTO.model_validate(client.post("/api/gateway/service-plugins/restart", json={"gateway_id": gateway_id,
                    "package_id": "stream.probe", "package_version": "1.0.0", "plugin_id": "streams",
                    "revision": slot.revision}).raise_for_status().json()["data"])

            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                pending = pool.submit(restart)
                with pytest.raises(ConnectionClosed):
                    socket.recv(timeout=8)
                # 空闲 SSE 的上游没有新帧；退役必须唤醒原读取，不能等后续数据。
                with pytest.raises(StopIteration):
                    next(lines)
                replacement = pending.result(timeout=25)
            assert replacement.state == "ready" and replacement.instance is not None, replacement
            assert replacement.instance.instance_id != instance_id
            evidence["replacement_instance"] = replacement.instance.instance_id
    with pytest.raises(InvalidStatus) as old:
        connect(socket_url, additional_headers=socket_headers, open_timeout=5)
    assert old.value.response.status_code == 409
    current_url = socket_url.replace(instance_id, replacement.instance.instance_id)
    with connect(current_url, additional_headers=socket_headers, open_timeout=5) as socket:
        assert json.loads(socket.recv(timeout=5))["instance_id"] == replacement.instance.instance_id
        socket.send("新连接只用新实例")
        assert socket.recv(timeout=5) == "新连接只用新实例"
    (context.artifacts_dir / "service-streams.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
