# 目录用途

`app/abstractions/` 存放服务之间共享的协议、接口和轻量 DTO 边界，用于隔离业务实现、编排实现和基础设施实现。

## 职责树

下面按协议面归类；Terminal 协议已按资源能力和执行观察职责放入子目录，其余协议仍位于根层。

```text
app/abstractions/
├── 会话边界
│   ├── session_changes.py          会话文件变更快照
│   ├── session_context.py          上下文查询、修订与 fork 传输
│   ├── session_orchestrator.py     会话编排调用端口
│   ├── session_resources.py        会话资源读取端口
│   ├── session_subagent.py         子 Agent 与 owner thread 创建端口
│   └── session_target.py           会话目标解析端口
├── 执行与消息边界
│   ├── background_message_bus.py   后台任务消息收发
│   ├── job_event_bus.py            Job 事件发布与订阅
│   ├── job_executor.py             Job 输入 owner 与执行状态
│   ├── job_service.py              Job 服务调用端口
│   ├── job_step_executor.py        Job 执行步骤端口
│   ├── pending_request_store.py    等待处理请求存储
│   ├── trace_event_sink.py         Trace 事件写入回执
│   ├── turn_history.py             Turn 历史读取端口
│   └── turn_terminal_status.py     Turn 与工具结果终态
├── Agent 与协作边界
│   ├── communication.py            Session 间通信端口
│   ├── custom_tool_context.py       自定义工具上下文
│   ├── embeddings.py               Embedding 计算端口
│   ├── internal_message.py         内部消息值对象
│   ├── team.py                     团队存储、扇出与协调端口
│   └── tool_selection.py           工具选择快照读取
├── 资源边界
│   ├── browser_resources.py        Browser 动作与传输
│   ├── resource_file_content.py    原字节/MIME 读取端口
│   └── workspace_resources.py      工作区资源快照与授权
├── terminal/                       Terminal 资源与执行观察协议
│   ├── execution_monitor.py        持久终端执行状态观察端口
│   └── resources.py                Terminal owner 与 PTY 传输协议
├── state_conflict.py               客户端状态冲突类型
└── __init__.py                     稳定的包级协议导出
```

# 可修改内容

- 可以新增或调整 Protocol、抽象接口和跨层调用所需的类型定义。
- 可以收紧协议参数，减少 `Any`、`kwargs` 等模糊边界。
- `resource_file_content.py` 只定义文件 owner 之间共享的原字节/MIME 值与读取端口；会话授权仍由各业务 owner 校验。

# 不可修改内容

- 不在本目录实现具体业务逻辑、运行时编排或基础设施访问。
- 不在协议中硬编码某个实现类或容器依赖。
- 不在本目录引入会造成业务层和基础设施层循环依赖的类型。

# 规范

- 协议命名应体现调用方语义，而不是具体实现细节。
- 类型尽量显式，避免把不透明字典作为长期接口。
- 修改协议后需要同步检查所有实现类和调用点。
- 模板示例；在整理 `AGENTS.md` 时请保留此行。
