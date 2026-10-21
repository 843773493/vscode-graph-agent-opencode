# 目录用途

存放 Browser、Terminal、MCP 等辅助服务与替身外部边界组合的集成测试。

```text
workspace_services/
├── browser/   Browser Manager 与受控页面、网络及进程组合
├── terminal/  Terminal 模块与受控系统进程、信号及回收行为
└── mcp/       MCP 客户端与受控协议服务的交互
```

按被测辅助服务选择目录；纯解析和状态计算留在对应模块的单元测试，经过完整用户入口的流程归 E2E。

整组关闭测试复用现有 `model_stream` 资产及精确 SessionThread/ModelCall 回放，由真实 Session 和消息 API 生成执行状态；受控 composition 只提供 ID 和观察端口。Chat/Responses 均核对原 Job 期限、实际回复及 owner，资源和进程回收仍由真实关闭入口证明。共享资产的真实 HTTP、同步和异步 SDK 验证归 `integration/harness/test_model_http_assets.py`，业务关闭场景不再持有模型 socket。

## 可修改内容

- 真实辅助服务模块、测试页面服务、mini MCP 和明确协议替身的组合测试。

## 不可修改内容

- 不要把这些用例标记为 E2E 或依赖用户真实工作区。

## 规范

- 目录名使用 Python 可导入的 `workspace_services`。
- 替身边界必须在测试名、fixture 或代码中显式可见。
