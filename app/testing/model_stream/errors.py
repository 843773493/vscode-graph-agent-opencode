from __future__ import annotations


class ModelStreamError(RuntimeError):
    """模型 stream 测试基础设施的统一错误基类。"""


class ModelStreamConfigError(ModelStreamError):
    """测试配置无效。"""


class ModelStreamAssetError(ModelStreamError):
    """stream asset 或 scenario manifest 无效。"""


class ModelStreamProtocolError(ModelStreamAssetError):
    """provider stream 协议未知、非法或尚未实现。"""


class ModelStreamMatchError(ModelStreamError):
    """请求无法唯一匹配 stream asset。"""
