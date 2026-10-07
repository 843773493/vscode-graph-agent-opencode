from __future__ import annotations

import asyncio
import hashlib
import json
from typing import cast

from app.abstractions.session_context import SessionContextAssemblySourceProtocol
from app.domain.itemized.selection import ContextSelectionEntry
from app.schemas.internal_v2.session import SessionDTO
from app.schemas.internal_v2.session_context import (
    SessionContextItemDTO,
    SessionContextReadRequest,
    SessionContextReadResultDTO,
)
from app.services.business.session_context_projection import (
    paginate_read_items,
    project_record_items,
)
from app.services.business.session_context_resource import (
    ParsedSessionContextResource,
    SessionContextCursorCodec,
    context_assembly_identity,
    require_session_context_revision,
    session_context_read_operation,
)


def _project_selection(entry: ContextSelectionEntry) -> dict[str, object]:
    """默认检查投影隐藏敏感正文及内部 hash，不改写封存 manifest。"""
    data = entry.to_dict()
    if entry.protection == "public":
        return data
    reference = cast(dict[str, object], data["ref"])
    for name in (
        "content_hash",
        "source_revision",
        "overlay_from_revision",
        "overlay_to_revision",
        "overlay_diff_hash",
    ):
        data[name] = None
        if name in reference:
            reference[name] = None
    reference.pop("tools", None)
    reference.pop("tool_policy", None)
    return data


class SessionContextAssemblyQueryService:
    """读取已封存的 assembly 目录与 history 投影。"""

    def __init__(self, *, assembly_source: SessionContextAssemblySourceProtocol) -> None:
        self._assembly_source = assembly_source

    async def read_assemblies(
        self,
        resource: ParsedSessionContextResource,
        request: SessionContextReadRequest,
        session: SessionDTO,
    ) -> SessionContextReadResultDTO:
        """只列 Saver 已封存快照；列表顺序不改变快照内部 selection。"""
        thread_id = self._require_main_thread_id(session)
        snapshots = await asyncio.to_thread(
            self._assembly_source.list_context_assemblies,
            session.session_id,
            thread_id,
        )
        revision = (
            "assemblies:"
            + hashlib.sha256(
                json.dumps(
                    sorted(snapshot.assembly_id for snapshot in snapshots)
                ).encode("utf-8")
            ).hexdigest()
        )
        require_session_context_revision(request.revision, revision)
        offset, char_offset = SessionContextCursorCodec.decode(
            request.cursor,
            resource_identity=resource.resource_identity,
            uri=resource.uri,
            scope=resource.scope,
            revision=revision,
            operation=session_context_read_operation(
                view=request.view,
                include=request.include,
                recent_rounds=request.recent_rounds,
                include_initial_goal=request.include_initial_goal,
            ),
            record_index=request.record_index,
            assembly_ref=request.assembly_ref,
        )
        items = [
            SessionContextItemDTO(
                kind="assembly",
                resource_identity=resource.resource_identity,
                uri=resource.uri,
                scope=resource.scope,
                assembly_ref=context_assembly_identity(
                    resource.resource_identity,
                    snapshot.assembly_id,
                ),
                data={
                    "session_id": snapshot.session_id,
                    "assembly_id": snapshot.assembly_id,
                    "plan_id": snapshot.plan_id,
                    "history_view_revision": snapshot.history_view_revision,
                    "source_overlay_epoch": snapshot.source_overlay_epoch,
                    "selection_count": len(snapshot.selection),
                    "target_format": snapshot.target_format,
                },
            )
            for snapshot in reversed(snapshots)
        ]
        return paginate_read_items(
            request=request,
            revision=revision,
            items=items,
            offset=offset,
            char_offset=char_offset,
            effective_record_count=len(items),
        )

    async def read_assembly(
        self,
        resource: ParsedSessionContextResource,
        request: SessionContextReadRequest,
        session: SessionDTO,
    ) -> SessionContextReadResultDTO:
        """冻结检查面不读取 active view，也不物化 request-only 正文。"""
        if request.assembly_ref is None:
            raise ValueError("assembly 查询缺少 assembly_ref")
        thread_id = self._require_main_thread_id(session)
        snapshots = await asyncio.to_thread(
            self._assembly_source.list_context_assemblies,
            session.session_id,
            thread_id,
        )
        snapshot = next(
            (
                candidate
                for candidate in snapshots
                if context_assembly_identity(
                    resource.resource_identity,
                    candidate.assembly_id,
                )
                == request.assembly_ref
            ),
            None,
        )
        if snapshot is None:
            raise KeyError(f"assembly_ref 不存在: {request.assembly_ref}")
        plan = snapshot.as_sealed_plan()
        # include 属于 projection 参数；不能用另一 include 的 cursor 跳过记录。
        include_hash = hashlib.sha256(
            json.dumps(sorted(set(request.include))).encode("utf-8")
        ).hexdigest()
        # assembly 不可变；检查面 revision 只绑定结构身份和 projection 参数。
        revision = f"assembly:{snapshot.assembly_id}:{include_hash}"
        require_session_context_revision(request.revision, revision)
        offset, char_offset = SessionContextCursorCodec.decode(
            request.cursor,
            resource_identity=resource.resource_identity,
            uri=resource.uri,
            scope=resource.scope,
            revision=revision,
            operation=session_context_read_operation(
                view=request.view,
                include=request.include,
                recent_rounds=request.recent_rounds,
                include_initial_goal=request.include_initial_goal,
            ),
            record_index=request.record_index,
            assembly_ref=request.assembly_ref,
        )
        messages, losses = await asyncio.to_thread(
            self._assembly_source.project_context_plan_to_history_with_diagnostics,
            session.session_id,
            thread_id,
            plan,
        )
        records = [message.model_dump(mode="json") for message in messages]
        items = [
            SessionContextItemDTO(
                kind="assembly",
                resource_identity=resource.resource_identity,
                uri=resource.uri,
                scope=resource.scope,
                assembly_ref=request.assembly_ref,
                data={
                    "session_id": snapshot.session_id,
                    "assembly_id": snapshot.assembly_id,
                    "plan_id": snapshot.plan_id,
                    "compiler_version": snapshot.compiler_version,
                    "provider_version": snapshot.provider_version,
                    "projector_id": snapshot.projector_id,
                    "projector_version": snapshot.projector_version,
                    "target_format": snapshot.target_format,
                    "history_view_revision": snapshot.history_view_revision,
                    "source_overlay_epoch": snapshot.source_overlay_epoch,
                    "selection_count": len(snapshot.selection),
                    "history_message_count": len(messages),
                    "history_loss_count": len(losses),
                    "assembly_loss_count": len(snapshot.loss),
                    "projection": "history",
                },
            )
        ]
        # selection 保留 tagged ref / omission / loss / typed detail owner；
        # ToolSet manifest 只是检查数据，不生成 history 工具定义 message。
        items.extend(
            SessionContextItemDTO(
                kind="selection",
                resource_identity=resource.resource_identity,
                uri=resource.uri,
                scope=resource.scope,
                assembly_ref=request.assembly_ref,
                record_index=entry.plan_ordinal,
                data=_project_selection(entry),
            )
            for entry in snapshot.selection
        )
        for item in project_record_items(
            resource_identity=resource.resource_identity,
            uri=resource.uri,
            scope=resource.scope,
            records=records,
            include=set(request.include),
            messages_only=True,
        ):
            if item.record_index is None:
                raise RuntimeError("history projection 缺少 message record_index")
            item.assembly_ref = request.assembly_ref
            item.data = {
                "projection": "history",
                "message_id": records[item.record_index]["id"],
            }
            items.append(item)
        items.extend(
            SessionContextItemDTO(
                kind="loss",
                resource_identity=resource.resource_identity,
                uri=resource.uri,
                scope=resource.scope,
                assembly_ref=request.assembly_ref,
                data={"projection": projection, "ordinal": ordinal, "loss": loss},
            )
            for projection, values in (("assembly", snapshot.loss), ("history", losses))
            for ordinal, loss in enumerate(values)
        )
        return paginate_read_items(
            request=request,
            revision=revision,
            items=items,
            offset=offset,
            char_offset=char_offset,
            effective_record_count=len(snapshot.selection),
            raw_message_count=len(messages),
        )

    @staticmethod
    def _require_main_thread_id(session: SessionDTO) -> str:
        if session.thread_id is None:
            raise RuntimeError(
                "SessionDTO 缺少权威 main thread_id: "
                f"session_id={session.session_id}"
            )
        return session.thread_id
