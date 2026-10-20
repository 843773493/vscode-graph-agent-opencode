"""远端条件写入只消费目标观察；传输丢响应不能伪报未执行。"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest

from app.core.hashing import sha256_hex
from app.core.resource_uri import resource_display_uri
from app.core.trace_middleware import (
    reset_current_gateway_id,
    reset_current_request_id,
    set_current_gateway_id,
    set_current_request_id,
)
from app.core.workspace_identity import load_or_create_workspace_id
from app.schemas.internal_v2.text_mutation import (
    CreateText,
    EditText,
    PatchText,
    TextMutationRequest,
)
from app.services.infrastructure.gateway_client_common import GatewayTransportConnection
from app.services.infrastructure.workspace_files.gateway_text_mutation import (
    GatewayTextMutation,
)
from app.services.infrastructure.workspace_files.paths import WorkspaceFilePaths
from app.services.infrastructure.workspace_files.text_journal import (
    load_text_mutation_snapshots,
)
from app.services.infrastructure.workspace_files.text_owner import (
    WorkspaceTextMutationOwner,
)

SOURCE = "gateway_0123456789abcdef0123456789abcdef"
TARGET = "gateway_123456789abcdef0123456789abcdef0"


@dataclass
class TransferCase:
    source: Path
    target: Path
    owner: WorkspaceTextMutationOwner
    target_owner: WorkspaceTextMutationOwner
    requests: list[httpx.Request]
    target_uri: str
    lose_response: bool = False
    wrong_owner: bool = False
    malformed_response: bool = False


@pytest.fixture
def transfer_case(tmp_path, monkeypatch, text_mutations_factory):
    source, target = tmp_path / "source", tmp_path / "target"
    source.mkdir()
    target.mkdir()
    source_id, target_id = load_or_create_workspace_id(source), load_or_create_workspace_id(target)
    gateway = GatewayTextMutation(workspace_id=source_id, connection=GatewayTransportConnection(gateway_url="http://gateway.test"),
                                  local_token_supplier=lambda: "local-secret")
    case = TransferCase(source, target, WorkspaceTextMutationOwner(
        paths=WorkspaceFilePaths(workspace=source, workspace_id=source_id, bindings=None), gateway=gateway),
        text_mutations_factory(target), [], resource_display_uri(scope="workspace", scope_id=target_id, kind="files",
            tail_segments=("root",), gateway_authority=TARGET))
    client = httpx.Client

    def respond(request):
        case.requests.append(request)
        payload = json.loads(request.content)
        assert payload["operation"] == "file_write"
        assert payload["mutation_target"] == payload["body"]["mutation_target"]
        assert payload["body"]["source_workspace_id"] == source_id
        assert payload["body"]["source_gateway_id"] == SOURCE
        assert request.headers["X-Local-Token"] == "local-secret"
        assert request.headers["X-Request-ID"] == "req-text-owner"
        mutation = TextMutationRequest.model_validate(payload["body"]["mutation"])
        token = set_current_gateway_id(TARGET)
        try:
            result = case.target_owner.execute_transfer(mutation).model_dump(mode="json", exclude_none=True)
        finally:
            reset_current_gateway_id(token)
        if case.lose_response:
            raise httpx.ReadTimeout("目标已执行，响应丢失", request=request)
        if case.wrong_owner:
            result["receipt"]["files"][0]["path"] = resource_display_uri(
                scope="workspace", scope_id=source_id, kind="files", tail_segments=("root", "file.txt"))
        return httpx.Response(200, headers={"X-Request-ID": "req-text-owner"},
                              json=[] if case.malformed_response else {"code": 0, "data": result, "request_id": "req-text-owner"})

    monkeypatch.setattr("app.services.infrastructure.workspace_files.gateway_text_mutation.httpx.Client",
                        lambda **kwargs: client(transport=httpx.MockTransport(respond), **kwargs))
    gateway_token, request_token = set_current_gateway_id(SOURCE), set_current_request_id("req-text-owner")
    try:
        yield case
    finally:
        reset_current_gateway_id(gateway_token)
        reset_current_request_id(request_token)


@pytest.mark.parametrize("operation", ["create", "edit", "patch"])
def test_remote_commands_publish_at_target_and_record_target_observation(transfer_case, operation):
    case = transfer_case
    (case.source / "file.txt").write_bytes(b"SOURCE DECOY")
    raw = b"\xef\xbb\xbfTARGET old\r\n"
    if operation != "create":
        (case.target / "file.txt").write_bytes(raw)
    version = "sha256:" + sha256_hex(raw)
    if operation == "create":
        command = CreateText(target=case.target_uri + "/file.txt", content="TARGET new\r\n", expected_version="absent")
        expected = b"TARGET new\r\n"
    elif operation == "edit":
        command = EditText(target=case.target_uri + "/file.txt", old_string="old", new_string="new", expected_version=version)
        expected = raw.replace(b"old", b"new")
    else:
        command = PatchText(target=case.target_uri,
            input="*** Begin Patch\n*** Update File: file.txt\n@@\n-\ufeffTARGET old\n+\ufeffTARGET new\n*** End Patch",
            expected_versions={"file.txt": version}, explanation="远端原字节版本")
        expected = raw.replace(b"old", b"new")
    authorized = []
    receipt = case.owner.execute(command, authorize=authorized.append)
    assert receipt["effect"] == "completed" and receipt["status"] == "success", receipt
    assert (case.target / "file.txt").read_bytes() == expected
    assert (case.source / "file.txt").read_bytes() == b"SOURCE DECOY"
    assert authorized == [case.target_uri + "/file.txt"]
    assert receipt["files"][0]["content_hash"] == "sha256:" + sha256_hex(expected)
    snapshot, = load_text_mutation_snapshots(json.dumps(receipt), workspace_root=case.source)
    assert snapshot["uri"] == case.target_uri + "/file.txt"
    assert snapshot["before_exists"] is (operation != "create")
    assert snapshot["before_content"] == ("" if operation == "create" else raw.decode().replace("\r\n", "\n"))
    assert snapshot["after_content"] == expected.decode().replace("\r\n", "\n")
    assert str(case.source) not in json.dumps(receipt) and str(case.target) not in json.dumps(receipt)


@pytest.mark.parametrize("failure", ["lost_response", "wrong_owner", "malformed_response"])
def test_after_dispatch_failure_is_unknown_without_invented_diff_or_replay(transfer_case, failure):
    case = transfer_case
    case.lose_response, case.wrong_owner = failure == "lost_response", failure == "wrong_owner"
    case.malformed_response = failure == "malformed_response"
    (case.source / "file.txt").write_bytes(b"SOURCE DECOY")
    receipt = case.owner.execute(CreateText(target=case.target_uri + "/file.txt", content="target published", expected_version="absent"))
    assert receipt["status"] == "error" and receipt["effect"] == "unknown" and receipt["retryable"] is False
    assert len(case.requests) == 1
    assert (case.target / "file.txt").read_bytes() == b"target published"
    assert (case.source / "file.txt").read_bytes() == b"SOURCE DECOY"
    assert receipt["target"] == receipt["files"][0]["path"] == case.target_uri + "/file.txt"
    assert receipt["files"][0]["state"] == "unknown" and receipt["files"][0]["expected_version"] == "absent"
    assert load_text_mutation_snapshots(json.dumps(receipt), workspace_root=case.source) == []


def test_source_permission_denial_precedes_transport_and_target_publication(transfer_case):
    case = transfer_case
    def deny(_uri):
        raise PermissionError("工具规则拒绝")
    with pytest.raises(PermissionError, match="工具规则拒绝"):
        case.owner.execute(CreateText(target=case.target_uri + "/file.txt", content="denied", expected_version="absent"), authorize=deny)
    assert case.requests == [] and not (case.target / "file.txt").exists()


def test_target_preflight_bounds_transfer_before_publication(transfer_case):
    case = transfer_case
    token = set_current_gateway_id(TARGET)
    try:
        transfer = case.target_owner.execute_transfer(TextMutationRequest.from_command(
            CreateText(target=case.target_uri + "/file.txt", content="large" * 2048, expected_version="absent"), max_response_bytes=4096))
    finally:
        reset_current_gateway_id(token)
    assert transfer.receipt.effect == "not_executed" and transfer.receipt.code == "output_limit_exceeded"
    assert not (case.target / "file.txt").exists()
