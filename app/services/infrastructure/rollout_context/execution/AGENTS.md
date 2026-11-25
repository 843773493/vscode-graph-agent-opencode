## 目录用途

承载 Turn、Execution、ModelCall 及终态收敛的持久化 owner；只通过 RolloutStorage 提供的事务与 domain contract 工作。

## 可修改内容

可修改 acceptance-time identity、attempt、model-call outcome 和 Turn lifecycle 的持久化编排。

## 不可修改内容

不得实现 LangChain/Provider wire 映射、业务规则、JSONL schema 细节、一次性 v1 import 或第二套 Turn/Execution 事实源。

## 规范

所有变更必须只使用当前 canonical 格式，保持事务原子性、幂等键和显式状态转移；源码注释使用中文，跨 owner 访问通过稳定 port 或 RolloutStorage 方法完成。

- `admission_reads.py` 按 Job 定位与完整 admission 校验共用同一只读快照，不在取得 execution_id 后重新开库；用户输入在同一快照返回完整 admission 和 canonical root，只读取并校验 acceptance/internal admission、冻结请求与因果游标；`internal_admission.py` 只拥有 internal 准入及其 SQLite/JSONL 事务。两者由 `store.py` 组合为同一执行 owner，调用方不得另建 writer 或沿旧模块转发。
- `queue.py` 同时拥有 Thread 暂停意图；只保存稳定 pause_id 和原 execution_id，终态联查 executions，不复制正文或执行结果。继续时以 pause_id CAS 清除意图，并在同一事务中将 continuation 放到普通待处理输入之前。
- 运行队列读取通过 `execution_queue_snapshot(validate_inputs=True)` 在同一只读事务校验完整冻结输入并取得快照；历史与显式迁移读取保留不要求投影已导入的边界，不再单独打开校验连接。
- 仅刷新队列版本时使用 `execution_queue_revision` 读取同一数据库的原 revision，不物化队列或读取输入正文；领取、恢复及派发仍沿完整输入校验，不新增版本缓存或计数账本。
