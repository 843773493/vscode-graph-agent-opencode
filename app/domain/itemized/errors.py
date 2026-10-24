"""itemized domain errors。"""


class CodeCarryingError(Exception):
    """携带闭合 ``code`` 的显式失败基类；``str(e)`` 恒为 ``[code] message``。

    只承载「code 携带」这一唯一事实：子类各自继承原有异常类型（如
    ``RuntimeError``/``ItemSchemaError``）与 code 语义，本类不叠加领域规则。
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"[{code}] {message}")
        self.code = code


class ItemSchemaError(ValueError):
    """v2 itemized value 不满足领域合同。"""


class FormatDispatchError(ItemSchemaError):
    """输入 envelope 与声明的 v2 format 不一致。"""


class InternalExecutionInterruptedError(RuntimeError):
    """持久中断先于自然终结；运行器必须转入取消收尾。"""

    def __init__(self, interrupt_request_id: str) -> None:
        self.interrupt_request_id = interrupt_request_id
        super().__init__(f"内部执行已接受中断: {interrupt_request_id}")
