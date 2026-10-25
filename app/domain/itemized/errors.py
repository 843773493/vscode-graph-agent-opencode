"""itemized domain errors。"""


def execution_failure_message(error: BaseException) -> str:
    """执行记录只携带原生 I/O 的结构化原因；原异常由诊断日志保留。

    没有系统错误字段的业务 TimeoutError 等异常继续使用各自消息合同。
    filename、filename2 和 strerror 均可能含本机位置，不作为业务错误正文。
    """
    if isinstance(error, OSError) and (
        error.errno is not None or error.filename is not None or error.filename2 is not None
    ):
        return f"执行系统 I/O 失败: error={type(error).__name__}, errno={error.errno}"
    return str(error)


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
