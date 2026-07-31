from __future__ import annotations

import secrets
import socket
from datetime import timedelta
from ipaddress import ip_address
from typing import Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, HttpUrl

from app.core.path_utils import get_gateway_root
from app.core.trace_middleware import get_request_id
from app.gateway.auth import verify_gateway_token
from app.gateway.credentials import FederationCredential, FederationCredentialStore
from app.schemas.public_v2.common import APIResponse

router = APIRouter(prefix="/api/gateway/device-connections", tags=["gateway-devices"])

DEVICE_CREDENTIAL_LIFETIME = timedelta(days=30)
DEVICE_PEER_PREFIX = "device:"


class CreateDeviceConnectionRequest(BaseModel):
    device_name: str = Field(min_length=1, max_length=80)
    gateway_url: HttpUrl


class DeviceConnectionDTO(BaseModel):
    connection_id: str
    device_name: str
    status: Literal["authorized", "expired"]
    credential_expires_at: str


class DeviceConnectionListDTO(BaseModel):
    items: list[DeviceConnectionDTO]


class DeviceAccessAddressDTO(BaseModel):
    url: str
    label: str
    is_loopback: bool


class DeviceAccessAddressListDTO(BaseModel):
    items: list[DeviceAccessAddressDTO]


class DeviceConnectionInfoDTO(BaseModel):
    gateway_url: str
    federation_token: str
    request_header: str
    manifest_url: str
    workspaces_url: str


class CreatedDeviceConnectionDTO(BaseModel):
    connection: DeviceConnectionDTO
    connection_info: DeviceConnectionInfoDTO


def _credential_store() -> FederationCredentialStore:
    return FederationCredentialStore(
        storage_path=get_gateway_root() / "credentials" / "federation.json"
    )


def _device_name(credential: FederationCredential) -> str | None:
    if not credential.peer_gateway_id.startswith(DEVICE_PEER_PREFIX):
        return None
    name = credential.peer_gateway_id.removeprefix(DEVICE_PEER_PREFIX).strip()
    return name or None


def _device_dto(credential: FederationCredential, *, device_name: str) -> DeviceConnectionDTO:
    return DeviceConnectionDTO(
        connection_id=credential.connection_id,
        device_name=device_name,
        status="expired" if credential.expired else "authorized",
        credential_expires_at=credential.expires_at.isoformat(),
    )


def _origin_candidate(raw_origin: str, *, label: str) -> DeviceAccessAddressDTO | None:
    parsed = urlsplit(raw_origin)
    if parsed.scheme not in {"http", "https"} or parsed.hostname is None:
        return None
    hostname = parsed.hostname.lower()
    return DeviceAccessAddressDTO(
        url=f"{parsed.scheme}://{parsed.netloc}",
        label=label,
        is_loopback=(
            hostname == "localhost"
            or hostname == "::1"
            or hostname.startswith("127.")
        ),
    )


def _preferred_network_addresses() -> set[str]:
    addresses: set[str] = set()
    route_probes = (
        (socket.AF_INET, ("192.0.2.1", 9)),
        (socket.AF_INET6, ("2001:db8::1", 9)),
    )
    for family, destination in route_probes:
        # TODO: Windows、Linux 与 macOS 没有统一的网卡枚举标准库接口；
        # 这里通过不发送数据的 UDP connect 读取系统选出的本机路由地址。
        try:
            with socket.socket(family, socket.SOCK_DGRAM) as route_socket:
                route_socket.connect(destination)
                addresses.add(str(route_socket.getsockname()[0]))
        except OSError:
            continue
    return addresses


@router.get("", response_model=APIResponse[DeviceConnectionListDTO])
async def list_device_connections(
    _: str = Depends(verify_gateway_token),
    request_id: str = Depends(get_request_id),
):
    items = [
        _device_dto(credential, device_name=device_name)
        for credential in _credential_store().list_all()
        if (device_name := _device_name(credential)) is not None
    ]
    items.sort(key=lambda item: item.credential_expires_at, reverse=True)
    return APIResponse(data=DeviceConnectionListDTO(items=items), request_id=request_id)


@router.post("", response_model=APIResponse[CreatedDeviceConnectionDTO])
async def create_device_connection(
    payload: CreateDeviceConnectionRequest,
    _: str = Depends(verify_gateway_token),
    request_id: str = Depends(get_request_id),
):
    device_name = payload.device_name.strip()
    if not device_name:
        raise HTTPException(status_code=422, detail="外部设备名称不能为空")
    connection_id = f"device_{secrets.token_hex(12)}"
    credential = _credential_store().issue(
        connection_id=connection_id,
        peer_gateway_id=f"{DEVICE_PEER_PREFIX}{device_name}",
        lifetime=DEVICE_CREDENTIAL_LIFETIME,
    )
    gateway_url = str(payload.gateway_url).rstrip("/")
    return APIResponse(
        data=CreatedDeviceConnectionDTO(
            connection=_device_dto(credential, device_name=device_name),
            connection_info=DeviceConnectionInfoDTO(
                gateway_url=gateway_url,
                federation_token=credential.token,
                request_header="X-BoxTeam-Federation-Token",
                manifest_url=f"{gateway_url}/api/gateway/federation/manifest",
                workspaces_url=f"{gateway_url}/api/gateway/federation/workspaces",
            ),
        ),
        request_id=request_id,
    )


@router.get(
    "/access-addresses",
    response_model=APIResponse[DeviceAccessAddressListDTO],
)
async def list_device_access_addresses(
    request: Request,
    _: str = Depends(verify_gateway_token),
    request_id: str = Depends(get_request_id),
):
    forwarded_host = request.headers.get("x-forwarded-host")
    forwarded_protocol = request.headers.get("x-forwarded-proto")
    forwarded_origin = (
        f"{forwarded_protocol or request.url.scheme}://{forwarded_host}"
        if forwarded_host
        else request.headers.get("origin")
    )
    request_origin = f"{request.url.scheme}://{request.url.netloc}"
    current = _origin_candidate(
        forwarded_origin or request_origin,
        label="当前访问地址",
    )
    if current is None:
        raise ValueError("无法从当前请求解析 Gateway 访问地址")

    parsed = urlsplit(current.url)
    port_suffix = f":{parsed.port}" if parsed.port is not None else ""
    candidates = [current]
    resolved_addresses = _preferred_network_addresses() | {
        result[4][0]
        for result in socket.getaddrinfo(
            socket.gethostname(),
            None,
            type=socket.SOCK_STREAM,
        )
    }
    for raw_address in sorted(resolved_addresses):
        address = ip_address(raw_address)
        if address.is_loopback or address.is_link_local or address.is_unspecified:
            continue
        host = f"[{address}]" if address.version == 6 else str(address)
        url = f"{parsed.scheme}://{host}{port_suffix}"
        if url == current.url:
            continue
        candidates.append(
            DeviceAccessAddressDTO(
                url=url,
                label="本机网络地址候选",
                is_loopback=False,
            )
        )
    return APIResponse(
        data=DeviceAccessAddressListDTO(items=candidates),
        request_id=request_id,
    )


@router.delete("/{connection_id}", response_model=APIResponse[DeviceConnectionListDTO])
async def revoke_device_connection(
    connection_id: str,
    _: str = Depends(verify_gateway_token),
    request_id: str = Depends(get_request_id),
):
    store = _credential_store()
    try:
        credential = next(
            item for item in store.list_all() if item.connection_id == connection_id
        )
    except StopIteration as error:
        raise HTTPException(status_code=404, detail="外部设备连接不存在") from error
    if _device_name(credential) is None:
        raise HTTPException(status_code=404, detail="外部设备连接不存在")
    store.remove(connection_id)
    items = [
        _device_dto(item, device_name=device_name)
        for item in store.list_all()
        if (device_name := _device_name(item)) is not None
    ]
    items.sort(key=lambda item: item.credential_expires_at, reverse=True)
    return APIResponse(data=DeviceConnectionListDTO(items=items), request_id=request_id)
