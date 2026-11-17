# 目录用途

`app/agents/` 存放 Agent 运行时装配、工具接入、中间件适配和与 LangGraph/DeepAgents 直接相关的运行时边界代码。

## 按职责查找

下图是职责索引，不表示方括号中的分组是实际目录。根目录里的模块仍直接位于 `app/agents/`；带 `/` 的条目才是物理子目录。

```text
app/agents/
├── [Agent 装配与模型调用]
│   ├── agent_factory.py          配置与绑定选择、单代图编译
│   ├── middleware_assembly.py    共享工具与执行中间件装配
│   ├── graph_binding.py、tool_node_adapter.py、model_call_graph.py
│   └── model_call_execution.py、sealed_assembly_dispatch.py
├── [Provider 配置、能力与路由]
│   ├── provider_api_mode.py、provider_capabilities.py
│   ├── model_capability_routing.py
│   └── providers/                 请求协议、消息投影和流响应适配
├── [工具策略、注册与运行]
│   ├── policy/                    工具能力、分组与策略解析
│   ├── tools/                     内置工具实现
│   ├── default_tools.py、builtin_tool_registry.py、custom_tools.py
│   ├── model_tool_schema.py、tool_identity.py、tool_set_producer.py
│   └── tool_result_text.py、tool_invocation_context.py
├── [模型上下文与摘要]
│   ├── checkpoint_recovery.py、manual_compaction_checkpoint.py、execution_checkpoint.py
│   └── summarization/              摘要计划、投影、重试和状态
├── [运行中间件与调用上下文]
│   ├── tool_confirmation_middleware.py
│   ├── structured_prompt_validation_middleware.py
│   ├── structured_tool_call_middleware.py、tool_output_middleware.py
│   └── llm_logging_middleware.py、upstream_request_trace.py
├── [指令与 Skill]
│   ├── instruction_producers.py、middleware_prompts.py、skill_catalog.py、skill_context.py
│   ├── agents_md_publication.py   AGENTS.md 文件登记与指令链发布
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

- `llm_logging_middleware.py` 的异步调用在原上下文结束 upstream capture，日志组装、校验与写盘交既有受保护 worker，完成后才返回；handler 失败时后续取消不得替换原异常。共享写锁仅覆盖写入和轮转，避免并发 writer 相互删除待统计文件，不在锁内组装日志。
- 请求日志复用消息模型的 JSON 序列化结果，不重复套日志模型校验和展开；upstream 正文在登记和更新入口脱敏，结束采集只固定 attempt 字段，不再次遍历正文。
- 与 LangGraph/DeepAgents 私有字段或私有方法交互时，应集中在适配器内，避免扩散到 business service。
- `workspace_backend.py` 仅保留框架内部虚拟工作区。会话附件与工具输出通过共享资源导航读取精确单文件 URI；附件 capability 来自 emitting model-call 的原 assembly，不能改为遍历会话私有目录或借用普通文件 relay。
- 运行时配置应通过 `ConfigService` 或显式参数传入。
- 后台工具依赖 `orchestration/background_work/` 的任务注册器与持久邮箱；CustomToolFactoryContext.background_message_bus 是扩展 factory 的既有公开字段名，其值为 BackgroundMailbox，不表示 Job 事件广播。
- `CustomToolFactoryContext.session_orchestrator` 是扩展 factory 的既有公开字段名，其值实现 `SessionMessageOrchestratorProtocol`；消息准入与派发由 `orchestration/session_messages.py` 实现，不代表 Session 创建、删除或 runtime 装配。
- `tool_identity.py` 单点定义注册工具的 `ToolDefinitionLike` 和名称提取；策略身份由 `policy/tool_capability.py` 推导，中间件和 ToolSet 注册不再声明同形类型或各自重算分组。
- `default_tools.py::build_default_tools` 只装配固定默认工具，协作子集归 `tools/collaboration.py`；仅传递实际消费的依赖，不转导出领域工厂。策略过滤、扩展工具和执行图仍由 `agent_factory.py` 装配。
- `middleware_assembly.py` 统一维护工具、Skill、压缩、路由和校验中间件的构造顺序；Skill 上下文登记、冻结目录安装与来源变化消费归 `skill_context.py`，压缩实例构造归 `summarization/factory.py`。工厂不再额外尝试装入空 Skill 中间件。
- `runtime/agent_runtime.py::assemble_agent` 在配置入口将 `tools.custom` 解析为 `ParsedCustomToolSpec`，图装配和 `custom_tools.py::build_custom_tools` 直接消费该类型，不重复解析原始配置。工具构造器只负责逐工具 options 注入、factory 结果及公开 schema 校验，不再包装单字段 Bundle。
- `tool_node_adapter.py` 的 `AgentToolNode` 统一适配 LangGraph 工具节点：在执行边界应用审批覆盖，结果仍配对原模型调用，并保留完整媒体内容；`model_call_graph.py` 调用 `adapt_agent_tool_node` 装入该节点。工具确认决策归 `tool_confirmation_middleware.py`，事件消费只通过适配器读取已注册工具。
- `tool_result_text.py` 统一提取工具输出文本和判定显式状态；Agent step 最终回答的匹配引号还原归 `execution_step/retry.py`。
- 产品级 Skill 源码统一位于 `resources/skills/`；自动目录只展示 Skill 的 `name`/`description`，显式文件导航只公布规范资源 URI，不暴露 DeepAgents 虚拟挂载或宿主路径。`skill_load(name=...)` 独占正文激活与来源追踪；普通目录导航与文件读取不产生这些副作用，Skill 文件可按公布的 URI 显式读取。
- 普通文件目标按用户裁定接受相对路径、目标机器原生绝对路径与资源 URI；三者经同一目标解析后执行，工作区外目标按实际 owner 能力访问或明确拒绝，禁止去除根或盘符后改投工作区。`.` 表示显式目标工作区根；DeepAgents 的 `/` 虚拟路径只存在于内部适配层，补丁内文件名仍只允许相对固定根。源码调试继续遵守其工作区能力边界。
- 失败时抛出明确错误，不要返回虚假的默认值。

- `execution_checkpoint.py` 只适配活动执行的 LangGraph 输入通道、失效 Pregel task 清理和最终回复替代消息；执行准入与 canonical 持久化仍归注入的 Saver owner，不在这里创建准入或另存消息。
- `manual_compaction_checkpoint.py` 是手动压缩的 LangGraph 检查点适配边界：读取快照并登记 `_force_cache_compaction` 请求。实际摘要生成归 `summarization/` 中间件，检查点持久化归注入的 checkpointer；不在此增加消息副本或另一套存储。

- `agents_md_publication.py` 登记工作区 AGENTS.md 文件并发布已接受版本的有序指令链，不管理 Agent 实例；`skill_catalog.py` 在目录构建与来源通知中调用它，冻结链的 CSM 安装归 `rollout_context/runtime/context_sources/agents_md_context.py`。
- `agent_factory.py::assemble_deep_agent` 装配工具并构建单代图，`assembly_mode="execution"` 返回单代图，`"tool_catalog"` 只返回工具注册列表且不绑定 Thread owner。模型候选、路由中间件与指令来源由 `runtime/agent_runtime.py::prepare_agent_model` 准备为 `AgentModelSetup`；配置读取、依赖收集和冻结工厂选择统一归该 runtime 模块的 `assemble_agent`，图复用与释放归 Thread tracker。
- `graph_binding.py` 只声明、校验和解析持久化的图 factory selector；真实线程归属由 `tool_invocation_context.py::ThreadRuntimeBinding` 在执行装配时绑定，两者不再包装成另一种 invocation binding。读取 selector 或注册 factory 不应加载工具执行和 Agent SDK。
- `instruction_producers.py` 定义指令来源及其发布绑定，生产入口 `register_instruction_producer` 将声明登记到唯一 CSM；base/delta、激活和待处理决策由 CSM 的来源生命周期链路生成。
- `tool_set_producer.py` 保存注册工具与结构化输出工具的冻结 schema，按原子 selection 生成模型可见的 ToolSet；结构化输出 schema 的 `None` 表示尚未绑定，空 tuple 表示已绑定为空，实际请求工具列表不参与反向构建。
