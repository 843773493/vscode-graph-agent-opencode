"""文本传输只核固定 URI 与目标观察，不定位 source 的实际文件。"""
from __future__ import annotations

from pathlib import PurePosixPath, PureWindowsPath

from app.core.resource_uri import (
    file_uri_child,
    parse_resource_uri,
    resource_display_uri,
)
from app.gateway.federation.errors import FederationError
from app.schemas.internal_v2.text_mutation import (
    CreateText,
    PatchText,
    TextMutation,
    TextMutationTransferDTO,
)
from app.services.infrastructure.resource_platform.virtual_resources.resolver import (
    ResourceUriResolveError,
)
from app.services.infrastructure.workspace_files.text_patch.parser import (
    identify_files_affected,
)


def mutation_file_versions(command: TextMutation) -> dict[str, str]:
    if not isinstance(command, PatchText):
        return {command.target: command.expected_version}
    names = identify_files_affected(command.input)
    if set(names) != set(command.expected_versions):
        raise ValueError("expected_versions 必须精确覆盖全部文件路径，包含移动的源与目的")
    result = {}
    for name in names:
        path = PurePosixPath(name)
        if not name or path.is_absolute() or PureWindowsPath(name).drive or ".." in path.parts or "://" in name:
            raise ValueError("补丁内文件名必须相对固定根")
        uri = file_uri_child(command.target, *path.parts)
        if uri in result:
            raise ValueError("补丁中的多个路径指向同一文件")
        result[uri] = command.expected_versions[name]
    return result


def qualify_text_transfer(transfer: TextMutationTransferDTO, command: TextMutation, gateway_id: str) -> TextMutationTransferDTO:
    expected = mutation_file_versions(command)
    root = parse_resource_uri(command.target, local_gateway_id=gateway_id)

    def qualify(uri: str) -> str:
        parsed = parse_resource_uri(uri, local_gateway_id=gateway_id)
        if (parsed.gateway_authority is not None or
                (parsed.scope, parsed.scope_id, parsed.kind) != (root.scope, root.scope_id, root.kind)):
            raise ResourceUriResolveError("scope_mismatch", "文本修改响应改变了固定目标")
        return resource_display_uri(scope=parsed.scope, scope_id=parsed.scope_id, kind=parsed.kind,
                                    tail_segments=parsed.tail_segments, gateway_authority=gateway_id)

    receipt = transfer.receipt
    if receipt.target is not None and qualify(receipt.target) != command.target:
        raise ValueError("文本修改响应改变请求目标")
    files = tuple(item.model_copy(update={"path": qualify(item.path)}) for item in receipt.files)
    snapshots = tuple(item.model_copy(update={"uri": qualify(item.uri)}) for item in transfer.snapshots)
    actual = {item.path: item.expected_version for item in files}
    observed = {item.uri for item in snapshots}
    if (len(actual) != len(files) or any(expected.get(uri) != version for uri, version in actual.items())
            or len(observed) != len(snapshots) or not observed.issubset(expected)):
        raise ValueError("文本修改逐路径回执越出声明或改变调用者预期版本")
    if receipt.effect in {"completed", "partial"} and (actual != expected or observed != set(expected)):
        raise ValueError("文本修改已发布响应缺少完整的声明或目标观察")
    if receipt.status == "success" and (receipt.effect != "completed" or any(item.state != "published" for item in files)):
        raise ValueError("文本修改成功响应没有逐文件确认发布")
    return transfer.model_copy(update={"receipt": receipt.model_copy(update={"target": command.target, "files": files}), "snapshots": snapshots})


def unconfirmed_text_mutation(command: TextMutation, error: BaseException) -> dict[str, object]:
    # 请求一旦交给传输，异常不能证明目标未执行；仅保留调用前已知的 URI/版本。
    return {
        "status": "error", "effect": "unknown", "owner": "federation" if isinstance(error, FederationError) else "resource_file_operations",
        "code": error.code if isinstance(error, FederationError) else "execution_failed", "target": command.target, "retryable": False,
        "error_type": type(error).__name__, "error": "目标执行结果未得到确认",
        "explanation": command.explanation if isinstance(command, PatchText) else "",
        "files": [{"path": uri, "operation": None if isinstance(command, PatchText) else "add" if isinstance(command, CreateText) else "update",
                   "state": "unknown", "expected_version": version, "exists": None, "content_hash": None,
                   "content_observation_error": "transport_unconfirmed"}
                  for uri, version in mutation_file_versions(command).items()],
        "recovery": "重新读取原目标并核实逐文件结果；取消或连接断开不代表目标已停止，禁止自动重放",
    }
