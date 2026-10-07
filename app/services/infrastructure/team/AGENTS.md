# 目录用途

存放同一 Session 内团队任务面板的持久化实现，负责所属 Session 节点 `teams/` 下任务快照和事件日志的原子读写。

# 可修改内容

- 团队快照、事件日志的文件存储。
- 经权威 Session catalog resolver 定位的团队存储路径校验和序列化。

# 不可修改内容

- 不实现成员权限、任务状态流转或 Agent 调度规则；不保存第二份成员 publication、readiness 或 activation 权威。
- 不调用模型、SessionOrchestrator 或 Agent 工具。

# 规范

- 团队数据只能写入所属 Session 的 `teams/`；先通过显式工作区的权威 catalog resolver 取得物理 Session 节点，不假设固定的 `sessions/{session_id}` 路径，不写工作区级 `.boxteam/teams/`。
- 同 Session 精确 main/child thread 成员及其就绪状态归既有 Session SQLite collaboration ledger，任务面板只消费其投影。
- 快照写入必须使用同目录临时文件原子替换。
- 文件损坏或 ID 非法时直接抛错，不得跳过或返回空对象。
