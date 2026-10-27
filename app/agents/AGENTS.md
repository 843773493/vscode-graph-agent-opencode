# 目录用途

`app/agents/` 存放 Agent 运行时装配、工具接入、中间件适配和与 LangGraph/DeepAgents 直接相关的运行时边界代码。

## 按职责查找

下图是职责索引，不表示方括号中的分组是实际目录。根目录里的模块仍直接位于 `app/agents/`；带 `/` 的条目才是物理子目录。

```text
app/agents/
├── [Agent 装配与模型调用]
│   ├── agent_factory.py、workspace_agents.py、deep_agent_stack.py
│   ├── graph_binding.py、graph_tool_adapter.py、model_call_graph.py
│   └── model_call_execution.py、sealed_assembly_dispatch.py
├── [Provider 配置、能力与路由]
│   ├── provider_api_mode.py、provider_capabilities.py
│   ├── model_capability_routing.py
│   └── providers/                 请求协议、消息投影和流响应适配
├── [工具策略、注册与运行]
│   ├── policy/                    工具能力、分组与策略解析
│   ├── tools/                     内置工具实现
│   ├── agent_tools.py、builtin_tool_registry.py、custom_tools.py
│   ├── model_tool_schema.py、tool_identity.py、tool_set_producer.py
│   └── tool_result_text.py、tool_invocation_context.py
├── [模型上下文与摘要]
│   ├── checkpoint_recovery.py、context_checkpoint_store.py
│   ├── context_compaction_adapter.py
│   └── summarization/              摘要计划、投影、重试和状态
├── [运行中间件与调用上下文]
│   ├── tool_confirmation_middleware.py
│   ├── structured_prompt_validation_middleware.py
│   ├── structured_tool_call_middleware.py、tool_output_middleware.py
│   └── llm_logging_middleware.py、upstream_request_trace.py
├── [指令与 Skill]
│   ├── instruction_producers.py、middleware_prompts.py、skill_catalog.py、skill_runtime.py
│   └── tools/skill_loading.py
└── [工作区适配]
    ├── workspace_backend.py        DeepAgents 内部虚拟工作区
    └── workspace_filesystem_tools.py  真实工作区文件工具接入
```

扩展模块时先沿所属职责查找现有 owner；新增物理子目录时同步添加本地 `AGENTS.md`，并把职责加入此索引。

# 可修改内容

- 可以新增或调整 Agent runtime、middleware、provider 和 checkpoint 适配器。
- 可以封装第三方 Agent 框架的私有结构访问，向业务层暴露清晰语义接口。
- 可以维护 Agent 工具注册与运行时配置解析。

# 不可修改内容

- 不要把 API 路由、业务服务编排或用户会话业务规则放在这里。
- 不要在这里硬编码环境变量值或用户 workspace 路径。
- 不要在这里实现前端展示、SSE 推送或数据库式持久化业务。

# 规范

- 与 LangGraph/DeepAgents 私有字段或私有方法交互时，应集中在适配器内，避免扩散到 business service。
- `workspace_backend.py` 仅保留框架内部虚拟工作区。会话附件与工具输出通过共享资源导航读取精确单文件 URI；附件 capability 来自 emitting model-call 的原 assembly，不能改为遍历会话私有目录或借用普通文件 relay。
- 运行时配置应通过 `ConfigService` 或显式参数传入。
- 产品级 Skill 源码统一位于 `resources/skills/`；自动目录只展示 Skill 的 `name`/`description`，显式文件导航只公布规范资源 URI，不暴露 DeepAgents 虚拟挂载或宿主路径。`skill_load(name=...)` 独占正文激活与来源追踪；普通目录导航与文件读取不产生这些副作用，Skill 文件可按公布的 URI 显式读取。
- 普通文件目标按用户裁定接受相对路径、目标机器原生绝对路径与资源 URI；三者经同一目标解析后执行，工作区外目标按实际 owner 能力访问或明确拒绝，禁止去除根或盘符后改投工作区。`.` 表示显式目标工作区根；DeepAgents 的 `/` 虚拟路径只存在于内部适配层，补丁内文件名仍只允许相对固定根。源码调试继续遵守其工作区能力边界。
- 失败时抛出明确错误，不要返回虚假的默认值。
