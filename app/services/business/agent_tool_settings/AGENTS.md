# 目录用途

管理工作区内按 Agent 生效的工具目录与能力开关。这里负责用户查询、策略约束和选择变更，不执行工具。

```text
agent_tool_settings/
├── catalog.py  AgentToolCatalog 复用原工具工厂、声明和 MCP publication；ToolCatalogReader 是共同读取接口
└── service.py  AgentToolSettingsService 投影执行／模型可见状态并校验、提交开关变更
```

## 可修改内容

- 工具管理目录、能力状态投影、选择请求及依赖约束。
- 与 ConfigService、工具定义 owner 和 ToolSelectionStore 的业务接线。

## 不可修改内容

- 不实现工具执行、Agent factory、模型调用或工具结果文件存储。
- 不复制工具策略或选择事实；策略沿用 ToolPolicyResolver，持久选择归 infrastructure/tool_selection_store.py。

## 规范

- API 从应用容器取得唯一 AgentToolSettingsService；成功更新仅返回请求涉及的工具，保持原错误与原子提交语义。
- 运行时工具定义与用户开关分开：禁用工具仍出现在管理目录中，允许用户重新启用。
- 目录读取统一使用 `list_tool_definitions`，同时返回配对的配置 snapshot 与可序列化定义；DTO 策略投影固定该 snapshot。不将管理定义称为已启用工具；选择更新复用本次状态投影中的策略锁定结果，提交后重新读取权威状态。
- 默认 Agent、缺失工具错误及策略投影均取自目录配对的 snapshot，不借外层 pinned 配置重新解释身份。详情只投影命中的定义；选择提交后只重读持久选择并投影本次变更项，不重建目录。
- 选择提交在已有 publication_lock 内核对配置 revision，配置已更新则拒绝写入；目录构造在锁外，不能为避免过期提交反转 catalog 与配置发布锁的顺序。
- 所有调用方直接使用唯一入口，不保留转发模块或类型别名，不在请求路径创建第二套服务。
- 派生文件工具通过 `derived_tools` 读取同一目录、配对配置和用户选择；准入检查执行与审批，无来源 seal 时将首次可见 ToolSet 冻结进原输入 detail。实际调用再次检查撤销，不改变来源冻结的模型 schema。

- 当前配置和一次 MCP publication 在已有共用 publication_lock 内捕获；factory、schema 导出均在锁外。缓存每个当前 Agent 一份纯定义，按配置／目录 revision 与 generation 失效；readiness 从当次 publication 投影。不缓存工具实例，不 acquire 执行 lease，不重复连接或 relist。
