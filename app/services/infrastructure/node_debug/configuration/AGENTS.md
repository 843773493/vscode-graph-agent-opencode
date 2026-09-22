# 目录用途

`app/services/infrastructure/node_debug/configuration/` 承载 Node Debug 调试方案配置的基础设施实现：方案配置的构造/持久化工厂、方案注册表与激活选择、启动 profile 运行时配置解析，以及供 `NodeDebugService` 继承的方案配置控制链路 Mixin。

# 可修改内容

- 可以维护方案配置 DTO 组装、方案注册表读写、激活/选择状态和启动 profile 解析。
- 可以维护 `configuration_control.py` 中 `NodeDebugConfigurationControlMixin` 的方案控制方法族（能力投影、profile 名称解析、方案列表/读取/创建/更新/激活/删除/导入/复制，以及运行中阻断断言与动作记录收口）；它以 Mixin 形式由宿主 `NodeDebugService` 继承。
- 可以维护本子包内模块之间的导入路径和实现细节。

# 不可修改内容

- 不得实现 API 路由、Agent 编排、断点 mutation 或进程生命周期决策。
- 不得保留旧顶层模块路径、re-export wrapper 或兼容导入别名。
- 不得静默吞掉方案持久化或配置解析错误，也不得返回虚假的方案默认值。

# 规范

- 文件按单一配置职责命名，只依赖断点领域模块、会话存储与上层 schema。
- 需要跨子包能力时通过明确的绝对模块路径导入，不反向依赖 `service.py`。
- 失败必须直接抛出详细错误；修改本目录后运行 `uv run ruff check` 和对应 Node Debug 专项测试。
- 代码注释使用中文，公开行为变更必须同步更新调用方和测试，不得建立双轨实现。
