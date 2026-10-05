# 目录用途

`provider/` 负责把 Saver 已绑定的 sealed plan、prefix、canonical/source 正文和 ToolSetRef/Provider manifest 纯投影为外部 Provider request 及其 hash preimage；延续现有 `native_request.py` 的完整请求投影职责。

# 可修改内容

- 可以实现唯一的 ToolSetRef 到 chat-completions/responses tools bridge。
- 可以消费 Saver 提供的封存输入，复用现有 mapping 和工具 bridge 编码完整请求及统一 preimage；不持有 canonical/source 事实。
- 可以校验 provider schema、manifest hash、availability 和 capability loss。

# 不可修改内容

- 不得读取 rollout storage 或当前 provider registry 猜测工具定义。
- 不得在 agents/providers 或 mapping 目录复制 ToolSetRef wire projector。

# 规范

- 输入只能来自 Saver 已绑定的 sealed plan/prefix/ToolSetRef 及其解析正文；缺失/受保护正文必须显式失败，不读取实时来源补全。
- bridge 不拥有 canonical item、history 或 LangChain message 事实。
