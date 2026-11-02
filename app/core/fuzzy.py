"""名称与路径召回使用的顺序字符匹配；评分由结果消费者负责。"""


def is_subsequence(query: str, value: str) -> bool:
    """输入已按调用方合同规范化；每个字符必须按顺序出现在同一字段。"""
    remaining = iter(value)
    return all(character in remaining for character in query)
