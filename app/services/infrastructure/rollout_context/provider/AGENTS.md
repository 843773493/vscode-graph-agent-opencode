# 目录用途

`provider/` 只负责把已 sealed 的 ToolSetRef/Provider manifest 编码为外部 provider request。

# 可修改内容

- 可以实现唯一的 ToolSetRef 到 chat-completions/responses tools bridge。
- 可以校验 provider schema、manifest hash、availability 和 capability loss。

# 不可修改内容

- 不得读取 rollout storage 或当前 provider registry 猜测工具定义。
- 不得在 agents/providers 或 mapping 目录复制 ToolSetRef wire projector。

# 规范

- 输入只能是 Saver 提供的 sealed ToolSetRef；缺失/受保护正文必须显式失败。
- bridge 不拥有 canonical item、history 或 LangChain message 事实。
