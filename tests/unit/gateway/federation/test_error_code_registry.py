"""联邦错误码闭集与 response wire 边界。"""

from __future__ import annotations

import ast
import asyncio
import re
from pathlib import Path

import pytest

from app.core.resource_uri import _GRAMMAR_REASON_CODES, ResourceUriGrammarError
from app.gateway.federation import errors
from app.gateway.federation.channel import (
    ChannelHandshake,
    FederationChannelSession,
)
from app.gateway.federation.errors import (
    CLOSE_CODE_INTERNAL_ERROR,
    CLOSE_CODE_POLICY_VIOLATION,
    CLOSE_CODE_PROTOCOL_ERROR,
    FEDERATION_GRANT_EXPIRED,
    FEDERATION_MALFORMED_FRAME,
    FEDERATION_TARGET_NOT_RESOLVABLE,
    FederationError,
    close_code_for,
)
from app.gateway.federation.identity import FederationPeerIdentity
from app.gateway.federation.protocol import FRAME_RESPONSE, FederationFrame
from app.services.infrastructure.resource_platform.virtual_resources.resolver import (
    RESOURCE_URI_RESOLVE_REASON_CODES,
    ResourceUriResolveError,
)


def test_addressing_error_domains_are_disjoint_and_reject_each_others_codes() -> None:
    domains = (
        (ResourceUriGrammarError, _GRAMMAR_REASON_CODES),
        (ResourceUriResolveError, RESOURCE_URI_RESOLVE_REASON_CODES),
        (FederationError, errors.FEDERATION_ERROR_CODES),
    )
    for constructor, codes in domains:
        for other_constructor, other_codes in domains:
            if constructor is other_constructor:
                continue
            assert codes.isdisjoint(other_codes)
            for code in other_codes:
                with pytest.raises(ValueError):
                    constructor(code, "跨 owner 拒绝码不得混用")


def test_federation_resolution_codes_match_the_normative_owner_table() -> None:
    spec = (
        Path.cwd()
        / "openspec/changes/add-unified-virtual-resource-addressing/specs/virtual-resource-addressing/spec.md"
    ).read_text()
    table = spec.split("- **联邦解析期拒绝码（第三套，独立闭集）**：", 1)[1].split(
        "\n\n", 1
    )[0]
    assert set(re.findall(r"`((?:federation-[a-z-]+)|target_[a-z_]+)`", table)) == {
        errors.FEDERATION_UNKNOWN_PEER,
        errors.FEDERATION_CHANNEL_CLOSED,
        errors.FEDERATION_TARGET_NOT_RESOLVABLE,
        errors.FEDERATION_TARGET_AMBIGUOUS,
        errors.FEDERATION_DEADLINE_EXCEEDED,
        errors.FEDERATION_TRANSIT_LIMIT_EXCEEDED,
    }


def test_production_error_constructors_use_registered_owner_codes() -> None:
    """遍历实际调用点；新增同义码或跨域字面量必须在构造前被审计发现。"""
    error_codes_by_constructor = {
        "ResourceUriGrammarError": _GRAMMAR_REASON_CODES,
        "ResourceUriResolveError": RESOURCE_URI_RESOLVE_REASON_CODES,
        "FederationError": errors.FEDERATION_ERROR_CODES,
    }
    constructor_names = tuple(error_codes_by_constructor)
    root = Path.cwd()
    dynamic_sites = {
        ("app/gateway/federation/channel.py", "FederationError", "code"),
        ("app/gateway/federation/rpc.py", "FederationError", "code"),
        (
            "app/services/infrastructure/resource_platform/sources/gateway_skill_sources.py",
            "ResourceUriResolveError",
            "detail['code']",
        ),
    }
    observed_dynamic_sites = set()
    for path in sorted((root / "app").rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        if not any(name in source for name in constructor_names):
            continue
        relative = path.relative_to(root).as_posix()
        tree = ast.parse(source, filename=relative)
        for node in ast.walk(tree):
            if (
                not isinstance(node, ast.Call)
                or not isinstance(node.func, ast.Name)
                or node.func.id not in error_codes_by_constructor
            ):
                continue
            code = (
                node.args[0]
                if node.args
                else next(
                    arg.value
                    for arg in node.keywords
                    if arg.arg in {"code", "reason_code"}
                )
            )
            choices = (
                (code.body, code.orelse) if isinstance(code, ast.IfExp) else (code,)
            )
            for choice in choices:
                if isinstance(choice, ast.Constant):
                    assert choice.value in error_codes_by_constructor[node.func.id], (
                        relative,
                        node.lineno,
                        choice.value,
                    )
                elif isinstance(choice, ast.Name) and choice.id.startswith(
                    "FEDERATION_"
                ):
                    assert node.func.id == "FederationError"
                    assert getattr(errors, choice.id) in errors.FEDERATION_ERROR_CODES
                else:
                    site = (relative, node.func.id, ast.unparse(choice))
                    assert site in dynamic_sites, (site, node.lineno)
                    observed_dynamic_sites.add(site)
    assert observed_dynamic_sites == dynamic_sites


class _Transport:
    async def send_text(self, message: str) -> None:
        del message

    async def receive_text(self) -> str | None:
        return None

    async def close(self, *, code: int, reason: str) -> None:
        del code, reason


class _Handler:
    async def handle(self, *, method, request, session, deadline_at):
        del method, request, session, deadline_at
        return {}


def _session() -> FederationChannelSession:
    return FederationChannelSession(
        transport=_Transport(),
        handshake=ChannelHandshake(
            channel_instance_id="chan_registry_test",
            channel_epoch=1,
            local_gateway_id="gateway_local",
            peer=FederationPeerIdentity(
                gateway_id="gateway_peer",
                connection_id="rgw_peer",
                public_key_pem="",
            ),
            credential_digest="0" * 64,
        ),
        handler=_Handler(),
    )


def test_every_registered_federation_constant_is_constructible_and_serialized() -> None:
    constants = {
        value
        for name, value in vars(errors).items()
        if name.startswith("FEDERATION_") and isinstance(value, str)
    }
    assert constants == errors.FEDERATION_ERROR_CODES

    for code in errors.FEDERATION_ERROR_CODES:
        error = FederationError(code, "registered")
        assert error.to_wire() == {"code": code, "message": "registered", "detail": {}}
        assert close_code_for(error) in {
            CLOSE_CODE_PROTOCOL_ERROR,
            CLOSE_CODE_POLICY_VIOLATION,
            CLOSE_CODE_INTERNAL_ERROR,
        }


@pytest.mark.parametrize(
    "code",
    [
        "made-up-code",
        "unsupported_view",
        "snapshot_changed",
        "read-snapshot-invalid",
        CLOSE_CODE_PROTOCOL_ERROR,
        [],
    ],
)
def test_constructor_rejects_unknown_and_other_domain_codes(code: object) -> None:
    with pytest.raises(ValueError, match="未登记"):
        FederationError(code, "rejected")  # type: ignore[arg-type]


def test_existing_close_code_groups_remain_stable() -> None:
    assert close_code_for(FederationError(FEDERATION_MALFORMED_FRAME, "bad")) == (
        CLOSE_CODE_PROTOCOL_ERROR
    )
    assert close_code_for(FederationError(FEDERATION_GRANT_EXPIRED, "expired")) == (
        CLOSE_CODE_POLICY_VIOLATION
    )
    assert close_code_for(
        FederationError(FEDERATION_TARGET_NOT_RESOLVABLE, "missing")
    ) == (CLOSE_CODE_INTERNAL_ERROR)


@pytest.mark.asyncio
async def test_registered_remote_wire_error_keeps_its_contract() -> None:
    session = _session()
    future: asyncio.Future[dict[str, object]] = (
        asyncio.get_running_loop().create_future()
    )
    session._pending["cor_registered"] = future

    session._dispatch_response(
        FederationFrame(
            frame_type=FRAME_RESPONSE,
            payload={
                "correlation_id": "cor_registered",
                "ok": False,
                "error": {
                    "code": FEDERATION_GRANT_EXPIRED,
                    "message": "grant expired",
                    "detail": {"kind": "read"},
                },
            },
        )
    )

    with pytest.raises(FederationError) as error:
        await future
    assert error.value.to_wire() == {
        "code": FEDERATION_GRANT_EXPIRED,
        "message": "grant expired",
        "detail": {"kind": "read"},
    }


@pytest.mark.asyncio
async def test_unknown_remote_wire_error_becomes_protocol_error_with_cause() -> None:
    session = _session()
    future: asyncio.Future[dict[str, object]] = (
        asyncio.get_running_loop().create_future()
    )
    session._pending["cor_unknown"] = future

    with pytest.raises(FederationError) as error:
        session._dispatch_response(
            FederationFrame(
                frame_type=FRAME_RESPONSE,
                payload={
                    "correlation_id": "cor_unknown",
                    "ok": False,
                    "error": {
                        "code": "read-snapshot-invalid",
                        "message": "owner rejection",
                        "detail": {"locator": "/private/path"},
                    },
                },
            )
        )

    assert error.value.code == FEDERATION_MALFORMED_FRAME
    assert error.value.detail == {"remote_code": "read-snapshot-invalid"}
    assert isinstance(error.value.__cause__, ValueError)
    assert close_code_for(error.value) == CLOSE_CODE_PROTOCOL_ERROR
    future.cancel()
