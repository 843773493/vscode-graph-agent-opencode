# 目录用途

`context_sources/` 保存 ContextSourceManager 及来源生命周期的运行时协调代码，负责把已由来源 owner 解析的资源状态转换为待提交的上下文增量。

# 可修改内容

- 可以维护 source identity、revision、snapshot/tracked 状态和待注入 delta 的内存协调。
- 可以接收受信 source owner 已验证的正文，并把结果交给唯一 ContextStore owner。

# 不可修改内容

- 不得自行扫描工作区、创建 watcher、写入 JSONL/SQLite 或决定业务 channel。
- 不得向模型暴露来源的宿主机路径、credential 或内部 locator。

# 规范

- snapshot 每次显式加载采用调用冻结版本，并停止同名追踪；不自动观察更新。tracked 的变化通过已注册观察事件或显式 observe 输入进入。
- Skill 的冻结正文候选与 snapshot/tracked 模式由同一控制事务接受；applied 仍只由 assembly seal 推进，失败不得清理原 pending 或提前切换模式。
- 同一来源的未发送变化必须从已应用 revision 合并成一个 delta；untrack 不移除已提交上下文。
- 生命周期机制只返回状态和释放结果，不发布业务事件。
- observe明确区分unchanged、baseline_restored、not_tracked与accepted，pending单独表示尚未封存的更新；持续更新入口必须核验tracked，禁止用布尔回执推断已生效。
- 名称索引按 source_kind 隔离；Skill 模式操作及同名唯一追踪约束只作用于 Skill，不能覆盖或停止同名 instruction。要求 tracked 的重复登记须在变更 descriptor／绑定和持久控制前核对已有状态。

- `tool_definitions.py` 只从实际 canonical 发现与已封存来源派生活动定义 batch；原 activation 是正文来源，提交与校验仍归 Saver，不登记第二个 CSM 控制 owner。
- `runtime_state.py`只编码显式状态发布intent；原canonical item保留每次发布事实，CSM按同一source/revision合并未封存变化，applied只由seal推进。冷恢复及rebuild从原owner冻结正文，不回读任务定义。
