# 目录用途

`app/agents/tools/debugging/` 组织 Agent 面向的 Node 源码调试工具，把输入协议、状态投影和工具工厂分开维护。

```text
debugging/
├── contracts.py  严格输入模型
├── payloads.py   调试状态脱敏、投影和结果序列化
├── registration.py 工具名称、输入模型、描述、动作及按需构造的唯一注册表
├── factory.py    受信调用绑定与调试动作编排
└── __init__.py   工厂和 schema 的统一公开入口
```

# 可修改内容

- 在 `contracts.py` 中调整 Agent 可见输入字段。
- 在 `payloads.py` 中维护状态字段投影、脱敏和工具结果格式。
- 在 `registration.py` 中维护唯一工具注册表；名称、输入模型、描述和动作绑定不得在其他模块复制，扩展调用只构造所选工具。
- 在 `factory.py` 中维护受信绑定与 NodeDebugService 调用顺序。
- 新增调试动作时更新输入模型与注册表；工具目录 schema 和单项构造都从注册表读取。

# 不可修改内容

- 不在此目录实现 Inspector 进程生命周期、DAP 传输或持久化；这些由 `app/services/infrastructure/node_debug/` 负责。
- 不允许模型输入覆盖受信 session、thread、PID、Inspector 端口或内部 action ID。
- 不得跳过变量值和输出文本的脱敏投影。

# 规范

- 保持 contracts → factory → payloads 的职责边界清晰，避免复制协议定义或结果组装逻辑。
- 包级公开入口只导出工具工厂与 schema 查询函数，内部 helper 保持模块私有。
- 错误需要保留明确归属，不能把调试运行时故障静默转换成成功结果。
- 模板示例；在整理 `AGENTS.md` 时请保留此行。
