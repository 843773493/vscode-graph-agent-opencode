# 目录用途

`app/runtime/` 是工作区后端的运行时组装层，负责把服务层与基础设施实例装配成 Agent 运行上下文，并提供在导入期会拉起重依赖的懒加载能力。它位于 `app/services/`（业务行为）与 `app/agents/`（Agent 构建）之间，不实现业务规则。

四个模块的职责：

- `agent_runtime.py`：声明 `AgentRuntimeDependencyProvider` 依赖提供协议，并提供 `build_session_agent_runtime` 与工具定义装配函数，把外部依赖收敛后交给 `app/agents/agent_factory`。
- `session_orchestrator.py`：`SessionOrchestrator`，把用户消息的创建与调度编排放到一条 `create_and_run` 链路上。
- `embeddings.py`：`LiteLLMEmbeddingComputer`，按 provider 配置构造远程 Embedding 调用。
- `chatgpt_auth.py`：ChatGPT OAuth provider 的凭据目录解析与从 Codex 原生凭据迁移。

# 可修改内容

- 可以新增、调整运行时装配函数、依赖提供协议与编排入口。
- 可以把导入期会拉起 langchain/langgraph/litellm 等重依赖的包继续收拢到本目录，作为单一懒加载边界。
- 可以维护 Embedding 计算与 ChatGPT OAuth 凭据处理的运行时逻辑。

# 不可修改内容

- 不在本目录实现业务规则或会话状态计算；这些属于 `app/services/`，本目录只做装配与编排转发。
- 不在本目录重复定义服务实例的创建与生命周期管理：服务与基础设施实例由 `app/container.py` 装配，本目录只消费依赖提供协议传入的对象。
- 不通过文件位置向上推导仓库根目录；需要全局目录时使用 `app.core.path_utils` 提供的路径函数。
- 不在装配失败时返回虚假默认值：缺少必需依赖（如 checkpointer）必须显式抛错。

# 规范

- 依赖提供协议优先于直接 import 具体实现；新增外部依赖时同步在协议与 `app/container.py` 装配处补齐，并移除旧的兼容分支（发现 `hasattr` / TODO 兼容分支应尽快消除）。
- 懒加载：需要重依赖的模块只在运行时真正使用时导入，不要提到模块顶层。
- 失败必须显式抛出包含上下文的异常，禁止静默降级。
- 本目录的单元测试放在 `tests/unit/runtime/`，外部边界（模型、认证服务）必须显式替换。
