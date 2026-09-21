# 目录用途

`app/services/infrastructure/node_debug/` 承载 Node Debug 运行时的基础设施实现。顶层只保留唯一入口 `service.py` 与跨领域共享运行时原语 `runtime_state.py`，其余按职责下沉到 `breakpoint/`、`configuration/`、`process/`、`session/` 四个子包。

# 可修改内容

- 可以维护 `service.py` 的 Node Debug 服务编排入口，以及 `runtime_state.py` 中所有子包共用的运行态协议与状态原语。
- 可以维护 `breakpoint/`（断点）、`configuration/`（方案配置）、`process/`（进程）、`session/`（会话）四个子包的实现。
- 可以维护本目录对应的单元测试和测试辅助代码，但公开 HTTP/Proto 契约必须由上层 schema 与 API 模块负责。

# 不可修改内容

- 不得在本目录实现 API 路由、Agent 编排或前端展示逻辑。
- 不得保留旧的顶层 `node_debug_*.py` 模块、re-export wrapper、模块别名或兼容导入路径。
- 不得把纯 API schema、协议生成物或跨域业务规则塞入本目录。
- 不得静默吞掉进程、会话或文件持久化错误，也不得返回虚假的默认状态。

# 规范

- 文件按单一 Node Debug 基础设施职责命名；跨模块依赖使用明确的包内模块路径。
- 新增职责优先落到已有子包；确需新子目录时必须为其补齐四段式 `AGENTS.md`。子包内模块不得反向依赖顶层 `service.py`。
- 会话路径必须通过统一会话路径解析器取得，禁止拼接固定的会话目录。
- 失败必须直接抛出详细错误；修改本目录后运行 `uv run ruff check`、`uv run compileall` 和对应 Node Debug 专项测试。
- 代码注释使用中文，公开行为变更必须同步更新调用方和测试，不得建立双轨实现。
