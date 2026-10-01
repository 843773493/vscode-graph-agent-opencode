"""itemized domain errors。"""


class ItemSchemaError(ValueError):
    """v2 itemized value 不满足领域合同。"""


class FormatDispatchError(ItemSchemaError):
    """输入 envelope 与声明的 v2 format 不一致。"""
