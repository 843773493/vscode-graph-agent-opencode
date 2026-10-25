# tests/unit/services/infrastructure

## 目录用途

存放基础设施服务的单元测试，例如配置服务、trace 事件存储等。

## 按职责聚合的测试子树

```text
infrastructure/
├── conftest.py            # 每项测试隔离的 EventChannelService
├── config/                # 配置策略、路径差异、监听、快照与 shadow lifecycle
├── events/                # typed 事件与 channel 生命周期/重放
├── file_tree_settings/    # 会话和工作区文件树快捷方式设置
├── gateway_clients/       # Gateway HTTP client 的就绪检查、上下文与 fork relay
├── mcp/                   # MCP catalog owner、binding、配置与工具指引
├── node_debug/            # Node Debug 进程身份、Inspector、会话和恢复测试
├── resource_platform/     # 资源观察、注册与虚拟资源
├── rollout_context/       # rollout/checkpoint 持久化及恢复职责
├── team/                  # 团队协作服务
├── tool_services/         # 工具目录、选择、输出存储与服务接口
├── trace_event_store/     # 事件采集、trace 存储、索引和恢复
├── turn_history/          # 回合历史索引读取与重建
├── workspace_activity/    # 工作区活动事件与游标
├── workspace_files/       # 工作区文件路径、读取写入、资源观察与持久操作
├── workspace_service_clients/ # Browser/Terminal 辅助服务 HTTP client
└── workspace_state_store/ # 工作区状态库的配置与组合职责
```

按同一生产职责的测试数量与共享 fixture 组织子目录；单个小型服务测试仍可留在本层。
每个子目录用自己的 `AGENTS.md` 说明 owner、边界和测试入口。

## 可修改内容

- 新增、修改、删除基础设施服务的单元测试。
- 使用临时目录隔离文件系统副作用。

## 不可修改内容

- 不要测试业务编排逻辑。
- 不要引入对外部网络或真实 LLM 的依赖。

## 规范

- 测试应快速、独立、可重复。
- 文件系统相关测试必须使用 tmp_path 或临时环境变量隔离。
