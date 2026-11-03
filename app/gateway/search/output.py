"""搜索输出的唯一字节准入；网络格式由调用边界提供。"""

from collections.abc import Callable

from app.gateway.search.scheduler import SearchJob, output_limited
from app.gateway.search.scope import SearchScopeError
from app.schemas.gateway.search import GatewaySearchScopeResolutionDTO
from app.schemas.search_federation import (
    MAX_SEARCH_OUTPUT_BYTES,
    FederatedSearchEventDTO,
    FederatedSearchSummaryDTO,
)


class SearchResponseEncoder:
    """在唯一编码边界计量，预留逐分支终态及汇总；只暂存下一帧。"""

    def __init__(self, jobs: list[SearchJob], scope: GatewaySearchScopeResolutionDTO, request_id: str, *,
                 encode: Callable[[FederatedSearchEventDTO], bytes], max_bytes: int | None = None) -> None:
        self._encode = encode
        self.limit = MAX_SEARCH_OUTPUT_BYTES if max_bytes is None else max_bytes
        self.used = 0
        self._branches = {job.key: output_limited(job) for job in jobs}
        self._sizes = {key: len(value.model_dump_json().encode()) for key, value in self._branches.items()}
        self._summary_size = self._pending_size = sum(self._sizes.values())
        self._pending_count = len(jobs)
        # 最长序号及计数只用于小型信封的预留；正文仅在 publish 中编码一次。
        sequence = 2 * len(jobs) + 2
        branch = next(iter(self._branches.values()))
        self._terminal_overhead = len(self._encode(FederatedSearchEventDTO(
            sequence=sequence, request_id=request_id, type="branch", branch=branch,
        ))) - self._sizes[branch.branch]
        self._end_overhead = len(self._encode(FederatedSearchEventDTO(
            sequence=sequence, request_id=request_id, type="end",
            summary=FederatedSearchSummaryDTO(state="incomplete", branches=[], returned_results=sum(job.request.limit for job in jobs)),
        ))) + max(0, len(jobs) - 1)
        self.frame = self._encode(FederatedSearchEventDTO(sequence=1, request_id=request_id, type="scope", scope=scope))
        if len(self.frame) + self._reserve(self._summary_size, self._pending_size, self._pending_count) > self.limit:
            raise SearchScopeError("output_bytes", "范围及原续点超过搜索流字节上限，请缩小范围", status_code=413)

    def _reserve(self, summary_size: int, pending_size: int, pending_count: int) -> int:
        return summary_size + self._end_overhead + pending_size + pending_count * self._terminal_overhead

    def publish(self, event: FederatedSearchEventDTO) -> bool:
        if event.type == "scope":
            self.used += len(self.frame)
            return True
        frame = self._encode(event)
        if event.type == "end":
            if self.used + len(frame) > self.limit:
                raise RuntimeError("搜索流终态超过已预留的输出预算")
            self.frame = frame
            self.used += len(frame)
            return True
        branch = event.branch
        assert branch is not None
        key = branch.branch
        size = self._sizes[key] if branch is self._branches[key] else len(branch.model_dump_json().encode())
        summary_size = self._summary_size + size - self._sizes[key]
        pending_size = self._pending_size + size - self._sizes[key]
        pending_count = self._pending_count
        if event.type == "branch":
            pending_size -= size
            pending_count -= 1
        if self.used + len(frame) + self._reserve(summary_size, pending_size, pending_count) > self.limit:
            return False
        self._branches[key], self._sizes[key] = branch, size
        self._summary_size, self._pending_size, self._pending_count = summary_size, pending_size, pending_count
        self.frame = frame
        self.used += len(frame)
        return True
