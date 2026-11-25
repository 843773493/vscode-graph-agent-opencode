# 目录用途

拥有 Agent 事件流的身份、结果契约、watchdog、事件消费与临时状态累计。

# 可修改内容

- contracts 定义事件源/结果/异常，identity 校验 session/job，reader 管理迭代与取消。
- model_events 识别 LangChain 模型 run 名称并汇总用量；内容块累计使用 `mapping/agent_content_mapper.py` 的共享纯映射；tool_events 归一化事件中的工具输入，协调执行状态和可信引用。工具输出的文本提取与状态判定共用 `app/agents/tool_result_text.py`。
- processor 按事件推进流程，只调用现有 writer、runtime 和业务端口。
- 文件工具回执经 tool_events 转为 `ObservedFileEdit`，显式携带 owner 观察到的 before/after；processor 将其交给 Changes recorder，不把整次编辑命名为 before 快照，也不重读当前磁盘推断后件。

# 不可修改内容

- 不构建 Agent，不读取 public service 私有字段，不写 JSONL/SQLite。
- 不定义 canonical/provider schema，不生成第二份持久化正文或 UI/history projector。

# 规范

- 消费者直接 import 真实 owner；禁止经 processor 转引契约或恢复旧 support shim。
- 队列、watchdog 和清理由 reader 单独负责；状态累计仅在本次事件消费期间存在。
- 工具 callback 失败沿统一错误出口一次取得摘要和诊断；保留真实原因，不伪造工具结果覆盖框架随后返回的配对消息。
