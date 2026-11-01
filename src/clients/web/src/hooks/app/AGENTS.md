# 目录用途

共享 AppContext／ComposerContext 契约、消费者 hook 及启动种子状态。每个 Thread 的实际装配由实例控制器与 Workbench EditorPane 完成。

# 可修改内容

- 上下文类型、对象、消费者及启动种子状态。
- 与这些契约直接相关的测试。

# 不可修改内容

- 不恢复全局当前会话的 AppProvider 聚合入口。
- 不实现 UI、后端规则或第二套业务状态。

# 规范

- 工作区、Session、Thread 作用域必须显式；错误显式交付调用方或可见状态。
- 修改后完成相关测试、真实类型检查及 `bun run --cwd src/clients/web build`。

- 模板示例；在整理 `AGENTS.md` 时请保留此行。
