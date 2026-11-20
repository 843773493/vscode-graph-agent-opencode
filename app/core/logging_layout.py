"""根据产品 Home 解析共享日志合同中的日志根目录。"""

from pathlib import Path

from boxteam_plugin_sdk.logging_contract import load_runtime_log_contract

from app.core.path_utils import get_boxteam_home


def get_runtime_log_root() -> Path:
    return load_runtime_log_contract().root_for(get_boxteam_home())
