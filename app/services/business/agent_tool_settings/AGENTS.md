# 目录用途

管理工作区内按 Agent 生效的工具目录与能力开关。这里负责用户查询、策略约束和选择变更，不执行工具。

```text
agent_tool_settings/
├── catalog.py  AgentToolCatalog 组合运行时定义和配置中的扩展声明；ToolCatalogReader 是共同读取接口
└── service.py  AgentToolSettingsService 投影执行／模型可见状态并校验、提交开关变更
```

## 可修改内容

- 工具管理目录、能力状态投影、选择请求及依赖约束。
- 与 ConfigService、运行时目录和 ToolSelectionStore 的业务接线。

## 不可修改内容

- 不实现工具执行、Agent factory、模型调用或工具结果文件存储。
- 不复制工具策略或选择事实；策略沿用 ToolPolicyResolver，持久选择归 infrastructure/tool_selection_store.py。

## 规范

- API 从应用容器取得唯一 AgentToolSettingsService；成功更新仅返回请求涉及的工具，保持原错误与原子提交语义。
- 运行时工具定义与用户开关分开：禁用工具仍出现在管理目录中，允许用户重新启用。
- 目录读取统一使用 `list_tool_definitions`，不将管理定义称为已启用工具；选择更新复用本次状态投影中的策略锁定结果，提交后重新读取权威状态。
- 所有调用方直接使用唯一入口，不保留转发模块或类型别名，不在请求路径创建第二套服务。
