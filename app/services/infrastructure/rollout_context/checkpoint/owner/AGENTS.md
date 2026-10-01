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

