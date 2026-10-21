## Why

长对话已经通过 canonical item 复用消息正文，但 checkpoint 的复杂 channel、assembly 请求/前缀以及 view 成员索引仍可能随每次提交重复增长。保留现有 SQLite 和恢复架构，通过不可变版本引用与增量恢复减少重复持久化，并用相同产品工作负载证明收益及恢复正确性。

## What Changes

- 核对已发布的 LangGraph 1.2.16 与 langgraph-checkpoint 4.3.0，升级低于目标的依赖；若实施时已更高则不回退，先验模型节点、消息 reducer、同步/异步和恢复合同。
- 在现有 SQLite 中保存按 owner、namespace、channel、version 唯一定位的不可变状态值；checkpoint 引用版本，未变化版本复用正文并跳过重复序列化，同键异内容明确失败。
- 评估并接入官方 DeltaChannel、消息 reducer 与序列化/恢复协议；采用基准加增量，消息正文继续归 canonical owner。先测现有正文复用后的真实剩余收益，迁齐写入、读取、重放、fork 与恢复。
- 将 assembly 的消息、工具、资源和前缀改为可共享的不可变引用与必要增量，同时减少 view 成员索引重复；保留历史规范化请求重建和 request_hash 校验。
- 在现有提交边界原子发布 checkpoint、状态引用、assembly、model call 和资源绑定；清理保护 retained checkpoint、基准、中间增量、assembly、资源快照与 fork 依赖。
- 按纵向切片移除旧写入/读取实现。实际结构改变才升级 schema；SQL 文件抽离单独实施，不构成本变更前置条件。
- 以真实产品入口生成工具循环、并发结果、摘要、重启恢复、深历史 fork 和回退状态，比较各表/正文/索引增长与写入/恢复耗时；性能长尾保留有测量证据的 TODO，不预先承诺压缩比例。

## Capabilities

### New Capabilities

- `execution-history-incremental-storage`：不可变状态版本复用、官方增量消息合同、assembly/view 共享、引用保护回收及收益验收。

### Modified Capabilities

- `rollout-checkpoint-storage`：checkpoint 的非 messages channel 正文由逐 checkpoint 复制改为不可变版本引用，保留完整快照恢复、namespace 隔离与失败透明语义。

## Impact

影响 `pyproject.toml`/`uv.lock`、图状态和模型节点适配、现有 RolloutCheckpointSaver、checkpoint channel 写读及 pending writes、rollout schema/migration、assembly/seal/detail、view 索引、fork/rewind/compaction、retention/GC 和产品验收。保留单一 SQLite/canonical 存储链路，不引入 PostgreSQL 服务、官方第二套 SQLite saver 或另一份完整消息历史。

该变更在 `add-itemized-rollout-context` 已实现的 canonical item、精确 Thread owner、sealed assembly 与 fork 身份合同上继续优化；不等待其性能 TODO 或归档。优先级是依赖/合同核对和通道版本复用在前，assembly/view 共享随后，基于这些引用的回收最后；现有数据正确性缺陷优先于性能调优。
