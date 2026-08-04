# 目录用途

镜像 `app/agents/`，存放 Agent 组装、Provider、中间件、策略和内置工具的单元测试。

## 可修改内容

- Agent 组件的隔离测试与测试专用 fake。
- `policy/`、`providers/`、`tools/` 等生产子模块对应的测试分区。

## 不可修改内容

- 不调用真实模型 Provider、真实网络或完整 Agent 运行链路。
- 不在此放 Gateway、API 或服务层测试。

## 规范

- 测试文件按主要被测的 `app/agents/` 模块命名和放置。
- 外部 Provider、工具和运行时依赖通过 pytest fixture 显式替换，失败不得静默忽略。
