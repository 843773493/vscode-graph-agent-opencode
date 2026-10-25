"""通信工具构造用的显式端口 probe；未配置返回值的调用必须失败。"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.schemas.internal_v2.communication import CommunicationReceipt
from app.schemas.internal_v2.session_context import SessionContextReadResultDTO

SOURCE_SESSION = "ses_019c390d6a707c858709afddc785bf93"
SOURCE_THREAD = "thr_019bb545cb1f7ddc999cc895fa6285bc"
TARGET_SESSION = "ses_019b9dffc29a779e8564d196f78bb0a6"
TARGET_THREAD = "thr_01a100c4a1897a2d8d40b568d23fcbe6"
WORKSPACE_ID = "22222222-2222-4222-8222-222222222222"


@dataclass
class CommunicationProbe:
    send_receipt: CommunicationReceipt | None = None
    wait_result: dict[str, object] | None = None
    read_result: SessionContextReadResultDTO | None = None
    error: Exception | None = None
    calls: list[tuple[str, dict[str, object]]] = field(default_factory=list)

    def _record(self, operation: str, arguments: dict[str, object]) -> None:
        self.calls.append((operation, arguments))
        if self.error is not None:
            raise self.error

    async def send(self, **arguments: object) -> CommunicationReceipt:
        self._record("send", arguments)
        if self.send_receipt is None:
            raise AssertionError("测试没有配置 communication send receipt")
        return self.send_receipt

    async def wait(self, **arguments: object) -> dict[str, object]:
        self._record("wait", arguments)
        if self.wait_result is None:
            raise AssertionError("测试没有配置 communication wait result")
        return self.wait_result

    async def read(self, **arguments: object) -> SessionContextReadResultDTO:
        self._record("read", arguments)
        if self.read_result is None:
            raise AssertionError("测试没有配置 communication read result")
        return self.read_result
