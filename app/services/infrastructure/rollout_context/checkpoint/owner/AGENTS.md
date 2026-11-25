# 目录用途

`owner/` 是 Saver 的 context owner 族：draft plan 生命周期、assembly detail/dispatch、
source overlay 与 CSM 控制状态、reconciliation ledger、resource activation 持久化，
以及把它们组合为唯一 context owner facade 的 provider projection 边界。

# 可修改内容

- 可以维护 draft plan registry、detail/dispatch owner 与 assembly seal 前的校验。
- 可以维护 source overlay runtime、CSM 控制状态与 reconciliation ledger 的 owner 端口。
- 可以维护 resource activation snapshot 在 Saver 侧的持久化 port。

# 不可修改内容

- 不得让多个文件并行保留同一 plan/assembly/view owner 事实。
- 不得直接实现 LangChain/Provider wire 编码或 v1 fallback。
- 不得为旧 import 路径提供 re-export 垫片或兼容别名。

# 规范

- context owner 只拥有 plan/assembly/view port，持久化细节归 durable 与 storage owner。
- 每个 owner 事实源唯一；发现双 owner 必须合并，不得并存。
- 详情GC保留sealed/terminal assembly及activation引用；引用检查和tombstone沿原线程写锁提交，物理删除在锁外执行并可重试。显式None namespace用于后台遍历该线程库的全部过期详情namespace，不物化空rollout。


- `runtime_execution.py` 的已接受用户／内部消息 reader 同次返回真实 admission 和 HumanMessage；消费者直接核原绑定，不先重复查询 admission。用户正文与准入沿 execution owner 的同一只读快照取得，内部 notice／seed／derived 仍保留冻结 preimage 和引用边界校验。
