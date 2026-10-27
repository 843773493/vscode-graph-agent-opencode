# 目录用途

本目录存放后端内部使用的 Pydantic 请求、响应和业务组装模型。公开跨进程协议以 `proto/` 为唯一来源；这些模型只负责 FastAPI 运行时校验和内部业务边界。

## 按数据流查找

```text
internal_v2/
├── __init__.py                    选定稳定 DTO 的包级导出
├── 服务入口与运行状态
│   ├── common.py                    公共枚举、API 响应和游标页
│   ├── agent.py / config.py         Agent 选择、配置更新与重载回执
│   ├── runtime.py / sse.py          运行状态、关闭结果与 SSE 文档
├── 会话、消息与历史
│   ├── session.py / session_status.py
│   │                                 会话创建与生命周期状态
│   ├── session_navigation/
│   │   ├── __init__.py               导航和生成 DTO 的显式导出
│   │   ├── models.py                 导航节点、面包屑和搜索 DTO
│   │   └── operations.py             导航操作请求与回执
│   ├── message.py / message_stream.py / turn.py
│   │                                 消息提交、实时流和 Turn 展示
│   ├── trace.py / llm_request_log.py
│   │                                 Trace 事件与模型请求日志
│   ├── session_changes.py / session_context.py
│   │                                 会话文件变更与上下文查询
│   ├── session_fork_transfer.py / session_interaction.py
│   │                                 Fork 传输与会话交互请求
│   ├── session_resource.py / attachment.py
│   │                                 会话资源动作与消息附件引用
│   └── goal.py                       会话 Goal 状态与操作
├── 执行调度与协作
│   ├── persistent_task.py            持久任务组件、定义和运行回执
│   ├── job.py / pending_request.py   Job 执行快照与待处理请求
│   ├── communication.py / team.py   跨 Session 通信与团队 DTO
│   ├── tool.py / tool_test.py        工具选择与工具测试结果
│   ├── mcp.py                        MCP 工具和 Server 目录 DTO
│   └── node_debug.py                 Node 调试请求与动作 DTO
└── 工作区与资源
    ├── workspace.py / workspace_observation.py
    │                                 工作区信息与观察订阅
    ├── workspace_file_operations.py / text_mutation.py
    │                                 文件集合操作与条件文本修改
    ├── workspace_resource.py / resource_navigation.py
    │                                 工作区资源归属与只读资源导航
    └── artifact.py                   内部 Artifact 描述
```

先按请求或响应所属的业务对象选择模型，再沿调用方确认其内部运行时语义。此树是文件职责索引，不代表跨进程协议来源；需要新增对外字段时仍先修改对应 `.proto`，也不要为了树形排列建立重复 DTO 或转发模块。

# 可修改内容

- 后端运行时需要的内部请求、响应和业务模型
- 与 JSON/SSE 兼容适配相关的 Pydantic 校验规则

# 不可修改内容

- 不得把本目录作为 TypeScript 或跨进程协议的生成来源
- 不得在这里定义 Gateway、Terminal 或 Browser 的跨进程传输绑定

# 规范

- 新增跨进程字段必须先修改对应 `.proto`，再在这里补充 JSON/FastAPI 运行时校验
- 保持字段名和现有 HTTP/SSE 线格式兼容
- 运行时错误必须明确抛出，不得用虚假默认值掩盖协议不匹配
