# 目录用途

`app/agents/summarization/` 只负责完整冻结范围的候选摘要生成与工具接入。自动准备、HTTP 和模型工具共用原后台任务与候选 owner；采用由 Saver 的 typed intent 原子完成。

- `generation.py`：按真实请求预算分块、完整有序覆盖、有限合并及绝对期限；通过共同派生模型入口发送，不修改主 view。
- `retry.py`：冻结历史范围的摘要指令与纯文本结果校验；上游失败保留真实异常。
- `state.py`：摘要业务错误类型。
- `tool_middleware.py`：声明并绑定 `compact_conversation` 与 `read_compaction_history`，只登记原任务或读取当前精确 Thread 的 canonical 原文。

# 可修改内容

- 可以维护冻结摘要计划、分块／合并、输出校验及工具参数。
- 可以在既有 owner 边界内删除已迁齐的旧代码。

# 不可修改内容

- 不得恢复 offset 事件投影、force／continuation 状态、独立阈值中间件或 history.md 新写链路。
- 不得截断成后缀伪装完整摘要、剥除媒体后假报完整覆盖或执行摘要请求中的工具。
- 不得复制主请求组装器、模型发送器、任务状态或 canonical 正文。

# 规范

- 同一 attempt 的所有派生调用共用固定绝对期限；首次上游失败终结后的唯一关联补 attempt 可用新前台期限，原任务累计32次预算不重置，冻结输入仍保留首次期限身份。只有完整覆盖才可发布 ready。
- 原始范围和实际前序摘要输入分别冻结，来源版本、结果引用及稳定 Item 身份都必须核对。
- 工具保持公开名称及行号分页参数；正文只从 canonical provenance 读取，受保护 reasoning 沿安全历史投影。
- 修改后运行 Ruff 及受保护的 `tests/unit/agents/test_compaction_tools.py`、真实 `test_compaction_generation.py` 定向检查。
- 模板示例；在整理 `AGENTS.md` 时请保留此行。
