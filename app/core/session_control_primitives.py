"""per-session ``session-control.sqlite`` 各垂直链路共享的形态原语。

本模块只承载跨子包共用的形态约束，不含任何表的 DDL、行投影或读写方法：

- ``SHA256_HEX_PATTERN``：sha256 小写 hex 形态（owner binding 的
  ``stable_prefix_hash``、operation lease 的 ``preimage_hash``、thread
  creation 的 ``artifact_manifest_hash`` 与通信 ``payload_hash`` 共用）。

在 session-control 各子包之间只允许这一份实现；子包一律从此处导入，
禁止各自复制正则（复制会让形态口径分叉）。
"""

from __future__ import annotations

import re

__all__ = [
    "SHA256_HEX_PATTERN",
]


# sha256 小写 hex 形态（session-control 跨族共同口径）。
SHA256_HEX_PATTERN = re.compile(r"^[0-9a-f]{64}$")
