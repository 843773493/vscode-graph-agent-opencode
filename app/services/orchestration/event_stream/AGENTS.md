# 目录用途

拥有 Agent 事件流的身份、结果契约、watchdog、事件消费与临时状态累计。

# 可修改内容

- contracts 定义事件源/结果/异常，identity 校验 session/job，reader 管理迭代与取消。
- model_events 累计已规范化的模型事件；tool_events 协调工具事件的执行状态和可信引用。
- processor 按事件推进流程，只调用现有 writer、runtime 和业务端口。

# 不可修改内容

- 不构建 Agent，不读取 public service 私有字段，不写 JSONL/SQLite。
- 不定义 canonical/provider schema，不生成第二份持久化正文或 UI/history projector。

# 规范

- 消费者直接 import 真实 owner；禁止经 processor 转引契约或恢复旧 support shim。
- 队列、watchdog 和清理由 reader 单独负责；状态累计仅在本次事件消费期间存在。
