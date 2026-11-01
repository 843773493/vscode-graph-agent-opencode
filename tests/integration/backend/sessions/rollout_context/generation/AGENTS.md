# 目录用途

验证真实 instruction producer、ToolSet policy、配置发布、thread generation、Runner 与 Saver 到封存请求的纵向合同。

# 可修改内容

- 配置刷新、generation 失效和 instruction 内容继承的集成测试。
- R03/R04/R06/R07 条件来源随真实 ToolSet 策略启用、移除、恢复及旧封存重试的测试。
- 对既有 Runner、factory 与 registry fixtures 的显式复用和本目录测试断言。

# 不可修改内容

- 不得复制既有 fixtures、构造第二套 owner 或伪造被测持久化结果。
- 不得将受控外部模型响应或受信 create_team 注册桩描述为真实 Provider、团队业务 E2E。
- 不得在本目录修改生产实现或恢复旧兼容链路。

# 规范

- 使用 pytest fixture 注入，直接复用既有 runtime_case、runner_case、factory_context 与 registry_db。
- 正式工作区与产物由 request.node.path 派生到同名 out/tests 路径；同文件参数的共享状态须在 fixture 装配前重建。
- 外部模型响应可受控；compiler、producer、CSM、ToolSet、Saver 和 Runner 使用生产实现。
- 配置来源按 source_details 核对逻辑层；pending 按 deltas 与实际 sealed policy 核对。
- 失败路径同时核对明确错误、待处理状态与已封存数据未被改写。

`test_activation_policy_turn_boundary.py`复用按当前文件隔离的source_case、model_stream与ASGI消息入口；修改真实工作区配置后等待原文件watcher和生命周期candidate applier，通过公开状态/事件核next_job发布、同Turn冻结及下一Turn采用新策略，冷Saver与sealed请求核owner/hash。不得用省略candidate_applier的直接reload替代此场景；它不代Gateway/Web、进程重启、同步SDK或完整9.7验收。
