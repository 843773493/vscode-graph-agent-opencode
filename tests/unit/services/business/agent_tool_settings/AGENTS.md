# 目录用途

镜像 app/services/business/agent_tool_settings/，验证 Agent 工具管理目录、状态投影和选择变更。

## 可修改内容

- test_catalog.py：运行时工具和扩展声明的目录合成、元数据与分组。
- test_service.py：Agent 解析、能力开关、依赖冲突、错误映射和事件循环响应。

## 不可修改内容

- 不执行真实工具、模型或外部网络请求。
- 不复制业务策略，不放宽选择提交、错误归属和并发顺序断言。

## 规范

- 依赖通过 pytest fixture 或构造参数注入；文件与配置使用隔离工作区。
- 输出镜像当前测试文件写入 out/tests/unit/services/business/agent_tool_settings/；历史产物留在原路径。
