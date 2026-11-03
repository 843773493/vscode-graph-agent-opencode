"""真实 Gateway 的具名出站故障装具；不替换查询、匹配、调度或 SSE 编码。"""

import asyncio
from dataclasses import asdict, dataclass, field
from typing import Annotated, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.gateway.auth import verify_gateway_token
from app.gateway.main import app
from app.gateway.routes import search
from app.gateway.search.scheduler import SearchBranchError, SearchJob
from app.gateway.search.transport import SearchTransport
from app.schemas.internal_v2.search import WorkspaceSearchResultDTO


class FaultRequest(BaseModel):
    mode: Literal["delay", "fail", "release"]
    categories: list[str] = ["catalog", "thread"]


@dataclass
class ReadObservation:
    workspace_id: str
    category: str
    request_id: str
    deadline: float | None
    received: bool = False
    cancelled: bool = False
    settled: bool = False


@dataclass
class Fault:
    request: FaultRequest
    gate: asyncio.Event = field(default_factory=asyncio.Event)
    reads: list[ReadObservation] = field(default_factory=list)


faults: dict[str, Fault] = {}
router = APIRouter(prefix="/_test/search")


@router.post("/{query}")
async def control(query: str, payload: FaultRequest, _: Annotated[str, Depends(verify_gateway_token)]):
    if payload.mode == "release":
        fault = faults[query]
        fault.request = payload
        fault.gate.set()
    else:
        if query in faults:
            faults[query].gate.set()
        faults[query] = Fault(payload)
    return {"mode": payload.mode}


@router.get("/{query}")
async def observations(query: str, _: Annotated[str, Depends(verify_gateway_token)]):
    return {"reads": [asdict(read) for read in faults[query].reads]}


class FaultSearchTransport(SearchTransport):
    async def _read(self, job: SearchJob, url: str, headers: dict[str, str]) -> WorkspaceSearchResultDTO:
        fault = faults.get(job.request.query)
        if fault is None or (job.key.category == "file" and "file" not in fault.request.categories):
            return await super()._read(job, url, headers)
        read = ReadObservation(job.key.owner.workspace_id, job.key.category, self.request_id, job.request.deadline_at)
        fault.reads.append(read)
        try:
            if job.key.category in fault.request.categories and fault.request.mode == "fail":
                raise SearchBranchError("owner_http_error", "显式测试故障：owner 读取失败", retryable=True)
            page = await super()._read(job, url, headers)
            read.received = True
            if job.key.category in fault.request.categories and fault.request.mode == "delay":
                await fault.gate.wait()
            return page
        except asyncio.CancelledError:
            read.cancelled = True
            raise
        finally:
            read.settled = True


# 只替换本测试进程的出站传输类，其余产品入口、真实后端和状态生成保持原实现。
search.SearchTransport = FaultSearchTransport
# 产品的静态页面挂载匹配所有剩余路径，具名测试入口必须先于它登记。
app.router.routes[0:0] = router.routes
