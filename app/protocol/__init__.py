"""公开协议运行时入口。"""

import sys

from .generated import boxteam as _generated_boxteam

# Python Protobuf 绑定按照 proto package 使用顶层 boxteam import。
# 这里只为应用内导入建立同一个包别名，不复制或修改生成代码。
sys.modules.setdefault("boxteam", _generated_boxteam)
