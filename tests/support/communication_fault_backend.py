"""进程外 harness 的通信 admission 屏障；不替换任何生产返回值或持久 owner。"""

from __future__ import annotations

import asyncio
import json
import os
import traceback
from dataclasses import asdict
from pathlib import Path

from app.main import app
from app.services.business.communication.ledger import CommunicationLedgerService
from app.services.infrastructure.gateway_communication_client import (
    GatewayCommunicationClient,
)
from app.services.mapping.itemized import carrier_dedup
from app.services.orchestration.communication.inbox_admission_worker import (
    InboxAdmissionTarget,
    InboxExecutionBinding,
)
from app.services.orchestration.communication.service import SessionCommunicationService
from app.services.orchestration.runtime_source_dispatcher import RuntimeSourceDispatcher

_bind = SessionCommunicationService._bind
_outgoing = CommunicationLedgerService.admit_outgoing_send
_operation = GatewayCommunicationClient.operation
_dispatch = RuntimeSourceDispatcher.dispatch


async def _pause(stage: str, value: object) -> None:
    directory = Path(os.environ["BOXTEAM_TEST_COMMUNICATION_BARRIER_DIR"])
    directory.mkdir(parents=True, exist_ok=True)
    (directory / ("barrier-" + stage + ".json")).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    await asyncio.Event().wait()


async def _before_source(self: SessionCommunicationService, target: InboxAdmissionTarget) -> InboxExecutionBinding:
    directory = Path(os.environ["BOXTEAM_TEST_COMMUNICATION_BARRIER_DIR"])
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "before-source.json").write_text(json.dumps(asdict(target.inbox), ensure_ascii=False, indent=2) + "\n")
    # 此处位于原 worker claim 与真实 source+wakeup admission 之间；父进程读取后硬杀。
    await asyncio.Event().wait()
    return await _bind(self, target)


async def _after_outbox(self: CommunicationLedgerService, **kwargs):
    result = await _outgoing(self, **kwargs)
    outbox = self._store.get_communication_outbox(kwargs["request"].send_operation_id)
    await _pause("after_outbox", asdict(outbox))
    return result


async def _after_receipt(self: GatewayCommunicationClient, **kwargs):
    result = await _operation(self, **kwargs)
    if kwargs["operation"] in {"send", "reply"}:
        # 真实 target 已提交 acceptance、原 HTTP response 已回源，但 source 尚未写 receipt。
        await _pause("after_receipt", {"communication_id": result["communication_id"], "receipt": result})
    return result


async def _after_job(self: SessionCommunicationService, target: InboxAdmissionTarget) -> InboxExecutionBinding:
    binding = await _bind(self, target)
    # Job/source/wakeup 均经原生产 owner 提交；此处只挡住 worker 的 inbox CAS。
    await _pause("after_job", {"communication_id": target.inbox.communication_id, "inbox": asdict(target.inbox), "binding": asdict(binding)})
    return binding


async def _after_wakeup(self: RuntimeSourceDispatcher, wakeup):
    await _pause("after_wakeup", {"communication_id": wakeup.source_id, "wakeup": asdict(wakeup)})
    return await _dispatch(self, wakeup)


if os.environ.get("BOXTEAM_TEST_COMMUNICATION_BARRIER_DIR"):
    stage = os.environ.get("BOXTEAM_TEST_COMMUNICATION_BARRIER_STAGE", "before_source")
    if stage == "before_source":
        SessionCommunicationService._bind = _before_source
    elif stage == "after_outbox":
        CommunicationLedgerService.admit_outgoing_send = _after_outbox
    elif stage == "after_receipt":
        GatewayCommunicationClient.operation = _after_receipt
        # 同进程承载 source/target；目标停在 acceptance，避免混入运行中 Job 的崩溃窗口。
        SessionCommunicationService._bind = _before_source
    elif stage == "after_job":
        SessionCommunicationService._bind = _after_job
    elif stage == "after_wakeup":
        RuntimeSourceDispatcher.dispatch = _after_wakeup
    else:
        raise ValueError(f"未知通信崩溃屏障: {stage}")

_dedup = carrier_dedup.superseded_tool_group_item_ids


def _trace_dedup(items):
    directory = Path(os.environ["BOXTEAM_TEST_COMMUNICATION_TRACE_DIR"])
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / "carrier-observations.jsonl").open("a") as stream:
        stream.write(json.dumps({"callers": traceback.format_stack(limit=12), "items": [item.to_dict() for item in items]}, ensure_ascii=False) + "\n")
    try:
        return _dedup(items)
    except ValueError:
        (directory / "carrier-failure.json").write_text(json.dumps({
            "traceback": traceback.format_exc(), "callers": traceback.format_stack(limit=15),
            "items": [item.to_dict() for item in items],
        }, ensure_ascii=False, indent=2) + "\n")
        raise


if os.environ.get("BOXTEAM_TEST_COMMUNICATION_TRACE_DIR"):
    carrier_dedup.superseded_tool_group_item_ids = _trace_dedup

__all__ = ["app"]
