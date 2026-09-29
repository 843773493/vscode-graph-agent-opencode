"""配置来源的 VRN 构造：config kind 的唯一构造点。

配置来源资源的 VRN 形态由 owner change
``add-unified-virtual-resource-addressing`` 的 requirement「配置来源寻址必须使用
config kind 且 sqlite 层不可寻址」定稿：

    boxteam://{scope}/{scope_id}/resources/config/{logical_source_name}

- 尾段取**逻辑资源名**（如 ``workspace_inline``），MUST NOT 取原始文件名
  （真实文件名含点号 ``.jsonc``，不在 VRN 闭合 charset 内）；尾段 MUST NOT 含点号，
  charset MUST NOT 放宽、MUST NOT 新增转义后门。
- ``layer`` 是 VRN 之外的**兄弟字段**，MUST NOT 塞进 VRN。
- ``sqlite`` 层 MUST NOT 编 VRN：``user``/``user_local``/``workspace`` 三层在 state
  store 存在时共享同一个 ``workspace.sqlite``（见 ``config_service._config_source``），
  单一 VRN 会立刻对应多个逻辑来源，故 ``sqlite`` 层不可寻址，返回 ``None``。

构造一律走 ``resource_display_uri``（构造即校验、拒绝裸拼接），不在此复述语法。
"""

from __future__ import annotations

from app.core.distribution_identity import load_distribution_id
from app.services.infrastructure.resource_platform.virtual_resources.grammar import (
    resource_display_uri,
)

__all__ = [
    "inline_config_source_vrn",
]


def inline_config_source_vrn(*, logical_name: str) -> str:
    """构造发行包内 ``inline`` 层配置来源的 VRN。

    config 来源中**只有 ``inline`` 层可寻址**：

    - ``inline`` 层是发行包内真实存在的 JSONC 文件（``configs/{logical_name}.jsonc``，
      经 ``resolve_config_resource_source`` 校验存在），有**稳定 disk 载体**，故有 VRN；
    - ``user``/``user_local``/``workspace`` 三层在 state store 存在时共享同一个
      ``workspace.sqlite``（``config_service._config_source`` 统一把 layer 改写为
      ``sqlite`` 并返回同一个 ``path``），把它映射成单一 VRN 会立刻产生「同一 URI 对应
      多个逻辑来源」的冲突（owner change 的 requirement「配置来源寻址必须使用 config
      kind 且 sqlite 层不可寻址」明述）。故这三层与 ``sqlite`` 层一并**不可寻址**
      （``vrn=None``）；该结论的依据是**共享边界载体**，与 ``user`` 是否在 VRN scope 闭集内无关。

    尾段取**逻辑资源名**（即 ``configs/{logical_name}.jsonc`` 去扩展名的部分），
    MUST NOT 取含点号的原文件名。``scope_id`` 取真实 ``distribution_id``
    （按 manifest 的 distribution 与 version 推导，缺失即 fail-closed，绝不回退 ``local``）。
    """
    return resource_display_uri(
        scope="inline",
        scope_id=load_distribution_id(),
        kind="config",
        tail_segments=(logical_name,),
    )
