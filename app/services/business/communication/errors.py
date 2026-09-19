"""跨 Session 通信合同的显式错误类型。

合同违反必须显式抛出并携带闭集错误码，绝不静默失败或返回伪造默认值。
"""

from __future__ import annotations

COMMUNICATION_CONTRACT_ERROR_CODES: frozenset[str] = frozenset(
    {
        "communication-target-not-main",
        "communication-payload-invalid",
        "communication-operation-id-invalid",
        "communication-preimage-conflict",
        "communication-id-conflict",
        "communication-reply-correlation-conflict",
        "wait-selector-invalid",
        "wait-timeout-out-of-range",
        "wait-until-invalid",
        "deadline-clock-unavailable",
        "deadline-record-invalid",
    }
)


class CommunicationContractError(Exception):
    """通信合同违反；code 必须属于闭集错误码，detail 携带中文明细。"""

    def __init__(self, code: str, detail: str) -> None:
        if code not in COMMUNICATION_CONTRACT_ERROR_CODES:
            raise ValueError(f"未知通信合同错误码: {code!r}")
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail
