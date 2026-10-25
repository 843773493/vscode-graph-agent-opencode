# tests/unit/services/infrastructure/tool_services

## 目录用途

存放 `app/services/infrastructure/` 工具目录、工具选择、工具输出存储和工具服务接口的单元测试。

## 可修改内容

- 工具 catalog 的元数据与扩展工具声明测试。
- 工具选择持久化、并发锁及错误处理测试。
- 工具输出持久化和公开工具服务接口测试。
- 使用 `tmp_path` 与测试替身构造的配置和工具数据。

## 不可修改内容

- 不执行真实工具，不调用真实 LLM 或外部网络。
- 不测试 Agent 工具实现和业务编排。

## 规范

- 依赖通过 pytest fixture 注入；文件系统状态隔离在临时路径中。
- 错误测试应保留错误类型、原因与提交边界断言。
