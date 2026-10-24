"""模型和目标工作区共用的文本修改入口；工具层只投影参数与回执。"""
from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable

import httpx

from app.core.path_utils import is_resource_uri_input
from app.core.resource_uri import parse_resource_uri, resource_display_uri
from app.core.trace_middleware import get_current_gateway_id, require_current_gateway_id
from app.gateway.federation.errors import FederationError
from app.schemas.internal_v2.text_mutation import (
    MAX_MUTATION_REQUEST_BYTES,
    CreateText,
    EditText,
    PatchText,
    TextMutation,
    TextMutationReceiptDTO,
    TextMutationRequest,
    TextMutationTransferDTO,
)
from app.services.infrastructure.resource_platform.virtual_resources.resolver import (
    ResourceUriResolveError,
)
from app.services.infrastructure.workspace_files.gateway_text_mutation import (
    GatewayTextMutation,
)
from app.services.infrastructure.workspace_files.paths import WorkspaceFilePaths
from app.services.infrastructure.workspace_files.text_journal import (
    load_text_mutation_snapshots,
    update_text_mutation_journal,
    write_text_mutation_journal,
)
from app.services.infrastructure.workspace_files.text_mutation import (
    FileSnapshot,
    TextMutationOutputLimitError,
    TextPublication,
    execute_text_mutation,
    resolve_text_target,
    text_mutation_failure,
)
from app.services.infrastructure.workspace_files.text_patch.executor import (
    PatchPreparation,
)
from app.services.infrastructure.workspace_files.text_patch.models import DiffError
from app.services.infrastructure.workspace_files.text_publication import (
    FilePreimage,
    WorkspaceFileConflictError,
)
from app.services.infrastructure.workspace_files.text_transfer import (
    mutation_file_versions,
    unconfirmed_text_mutation,
)

logger = logging.getLogger(__name__)


class WorkspaceTextMutationOwner:
    def __init__(self, *, paths: WorkspaceFilePaths, gateway: GatewayTextMutation | None = None) -> None:
        self._paths, self._gateway = paths, gateway

    def execute(self, request: TextMutation, *, authorize: Callable[[str], None] | None = None) -> dict[str, object]:
        routed = self._routed_command(request)
        if routed is None:
            return self._execute_local(request, authorize=authorize)
        # 源端只核声明 URI 和本次工具规则；绝不以本机路径参与对端解析。
        for uri in mutation_file_versions(routed):
            if authorize is not None:
                authorize(uri)
        payload = TextMutationRequest.from_command(routed)
        if len(payload.model_dump_json(exclude_none=True).encode("utf-8")) + 4096 > MAX_MUTATION_REQUEST_BYTES:
            raise TextMutationOutputLimitError("文本修改请求超过传输预算，尚未发送")
        try:
            transfer = self._gateway.execute(payload)
        except (OSError, RuntimeError, ValueError, httpx.HTTPError, FederationError) as error:
            logger.exception("远端文本修改未得到有效回执，保留原目标核实")
            result = unconfirmed_text_mutation(routed, error)
            snapshots = []
        else:
            result = transfer.receipt.model_dump(exclude_none=True)
            snapshots = [item.model_dump(exclude_none=True) for item in transfer.snapshots]
        try:
            journal_id = write_text_mutation_journal(snapshots, workspace_root=self._paths.workspace)
            update_text_mutation_journal(journal_id, result.get("files", []), observations={}, workspace_root=self._paths.workspace)
        except (OSError, RuntimeError, ValueError) as error:
            # 目标效果不能被源端 journal 失败改报为“未执行”；事件消费者会明确报缺 journal。
            logger.exception("远端文本回执无法保存到源会话")
            result.pop("journal_id", None)
            result.update(status="error", journal_error=type(error).__name__)
        else:
            result["journal_id"] = journal_id
        return result

    def _routed_command(self, request: TextMutation) -> TextMutation | None:
        if not is_resource_uri_input(request.target):
            return None
        parsed = parse_resource_uri(request.target, local_gateway_id=get_current_gateway_id())
        if parsed.gateway_authority is None and not (parsed.scope == "workspace" and parsed.scope_id != self._paths.workspace_id):
            return None
        if parsed.scope != "workspace" or parsed.kind != "files" or self._gateway is None:
            raise ResourceUriResolveError("capability_denied", "此目标尚无已授权的文本写入 owner")
        return request.model_copy(update={"target": resource_display_uri(
            scope=parsed.scope, scope_id=parsed.scope_id, kind=parsed.kind, tail_segments=parsed.tail_segments,
            gateway_authority=parsed.gateway_authority or require_current_gateway_id("文本修改目标"),
        )})

    def execute_transfer(self, payload: TextMutationRequest) -> TextMutationTransferDTO:
        """目标 HTTP 入口只执行本机固定 owner，禁止递归 relay。"""
        command = payload.command
        if len(payload.model_dump_json(exclude_none=True).encode("utf-8")) > MAX_MUTATION_REQUEST_BYTES:
            result = text_mutation_failure(TextMutationOutputLimitError("文本修改请求超过目标预算，尚未发布"))
        else:
            try:
                result = self._execute_local(command, max_journal_bytes=max(1, payload.max_response_bytes - 1024))
            except (OSError, RuntimeError, ValueError) as error:
                result = text_mutation_failure(error)
        snapshots = []
        if result.get("journal_id") is not None:
            try:
                snapshots = load_text_mutation_snapshots(json.dumps(result), workspace_root=self._paths.workspace)
            except (OSError, RuntimeError, ValueError) as error:
                logger.exception("目标文本修改已执行但观察 journal 无法读取")
                result = unconfirmed_text_mutation(command, error)
        transfer = TextMutationTransferDTO(receipt=TextMutationReceiptDTO.model_validate(result), snapshots=snapshots)
        if len(transfer.model_dump_json().encode("utf-8")) + 512 > payload.max_response_bytes:
            transfer = TextMutationTransferDTO(receipt=TextMutationReceiptDTO.model_validate(
                unconfirmed_text_mutation(command, TextMutationOutputLimitError("实际后件超过回执预算"))))
        return transfer

    def _execute_local(self, request: TextMutation, *, authorize: Callable[[str], None] | None = None,
                       max_journal_bytes: int | None = None) -> dict[str, object]:
        if isinstance(request, PatchText):
            preparation = PatchPreparation(request.input)
            try:
                result = execute_text_mutation(
                    paths=self._paths, target=request.target, declared=preparation.declared,
                    expected_versions=request.expected_versions, prepare=preparation.prepare,
                    explanation=request.explanation, authorize=authorize, max_journal_bytes=max_journal_bytes,
                )
            except ValueError as error:
                # V4A 格式错误保留公开类型；内容版本冲突不归入补丁语法。
                if type(error) is ValueError:
                    raise DiffError(str(error)) from error
                raise
            result["fuzz"] = preparation.fuzz
            return result
        actual, uri = resolve_text_target(self._paths, request.target)
        relative = actual.relative_to(self._paths.workspace).as_posix()

        def prepare(snapshots: dict[str, FileSnapshot]) -> list[TextPublication]:
            snapshot = snapshots[relative]
            content = _create(request, snapshot.before) if isinstance(request, CreateText) else _edit(request, snapshot.before)
            return [TextPublication(snapshot, content, "add" if isinstance(request, CreateText) else "update")]

        result = execute_text_mutation(
            paths=self._paths, target=".", declared=[relative],
            expected_versions={relative: request.expected_version}, prepare=prepare, authorize=authorize, max_journal_bytes=max_journal_bytes,
        )
        result["target"] = uri
        return result

    async def execute_async(self, request: TextMutation, *, authorize: Callable[[str], None] | None = None) -> dict[str, object]:
        fixed = self._routed_command(request)
        if fixed is None:
            _, uri = resolve_text_target(self._paths, request.target)
            fixed = request.model_copy(update={"target": uri})
        # 生命周期留在调用者事件循环；工作线程继续记录实际发布结果。
        try:
            return await asyncio.to_thread(self.execute, fixed, authorize=authorize)
        except asyncio.CancelledError as error:
            receipt = unconfirmed_text_mutation(fixed, error)
            receipt.update(code="cancellation_requested", error="文件调用等待已取消，目标尚未确认停止")
            # 保留标准 CancelledError，asyncio.timeout 才能正确收敛自身的超时。
            raise asyncio.CancelledError(TextMutationReceiptDTO.model_validate(receipt)) from error


def _create(request: CreateText, before: FilePreimage | None) -> bytes:
    if request.expected_version != "absent" or before is not None:
        raise WorkspaceFileConflictError("创建文件必须明确要求 expected_version=absent")
    return request.content.encode("utf-8")


def _edit(request: EditText, before: FilePreimage | None) -> bytes:
    if before is None:
        raise FileNotFoundError("精确编辑的目标文件不存在")
    if not request.old_string or request.old_string == request.new_string:
        raise ValueError("old_string 必须非空且与 new_string 不同")
    original = before.content.decode("utf-8")
    occurrences = original.count(request.old_string)
    if occurrences == 0 or (not request.replace_all and occurrences != 1):
        raise ValueError(f"精确编辑匹配数量无效: occurrences={occurrences}; 需要唯一匹配或明确 replace_all")
    return original.replace(request.old_string, request.new_string, -1 if request.replace_all else 1).encode("utf-8")
