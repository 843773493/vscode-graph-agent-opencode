# 目录用途

`storage/` 负责 v2 JSONL、catalog、commit 和恢复边界的基础持久化适配。

# 可修改内容

- 可以实现 JSONL 编解码、SQLite 索引和崩溃恢复的基础组件。
- 可以调用 `app/domain/itemized/` 的值对象与 JCS hash。

# 不可修改内容

- 不得实现 LangChain、Provider wire、业务规则或一次性 v1 import reader。
- 不得建立第二份 canonical payload 事实源。

# 规范

- JSONL item 必须不可变、使用 RFC 8785 JCS；offset/length 必须与唯一权威 commit 状态一致。
- 失败必须明确报错，不得静默跳过损坏记录。
