# 目录用途

验证真实配置发布经 R01 producer、thread generation、Runner 和 Saver 到封存请求的纵向合同。

# 可修改内容

- 配置刷新、generation 失效和 R01 内容继承的集成测试。
- 对既有 Runner fixtures 的显式复用与本目录测试断言。

# 不可修改内容

- 不得复制 Runner fixtures、构造第二套 owner 或伪造被测持久化结果。
- 不得将受控外部模型响应描述为真实 Provider E2E。
- 不得在本目录修改生产实现或恢复旧兼容链路。

# 规范

- 使用 pytest fixture 注入，直接复用既有 runtime_case 和 runner_case。
- 正式工作区与产物由 request.node.path 派生到同名 out/tests 路径。
- 外部模型响应可受控；compiler、producer、CSM、Saver 和 Runner 使用生产实现。
- 失败路径同时核对明确错误、待处理状态与已封存数据未被改写。
