## 目录用途

承载 v2 rollout 的逻辑 pruning 操作；逻辑 compaction/view 边界由 checkpoint adapter 调用统一事务。

## 可修改内容

可修改 checkpoint/view pruning 的候选、引用保护和元数据操作流程。

## 不可修改内容

不得实现 canonical domain schema、LangChain/Provider 映射、业务规则、一次性 v1 import 或第二套持久化事实源。

## 规范

所有操作必须使用 RolloutStorage 的锁、offset 不变量和显式状态提交；已提交 canonical JSONL 字节、item sequence 和 locator 永久不可变，不提供物理回收或重写路径。源码注释使用中文。
