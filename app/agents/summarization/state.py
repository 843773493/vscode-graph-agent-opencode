"""摘要生成的业务错误类型。"""

class SummaryTextError(ValueError):
    """摘要响应没有可见正文。"""


class SummaryGenerationError(RuntimeError):
    """第三方摘要器明确返回生成失败。"""


class CompactionReductionError(RuntimeError):
    """生成的摘要没有缩短本次上下文。"""
