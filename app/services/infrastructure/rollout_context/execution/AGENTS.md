## 目录用途

承载 v2 Turn、Execution、ModelCall 及终态收敛的持久化 owner；只通过 RolloutStorage 提供的事务与 domain contract 工作。

## 可修改内容

可修改 acceptance-time identity、attempt、model-call outcome 和 Turn lifecycle 的持久化编排。

## 不可修改内容

不得实现 LangChain/Provider wire 映射、业务规则、JSONL schema 细节、一次性 v1 import 或第二套 Turn/Execution 事实源。

## 规范

所有变更必须保持 v2-only、事务原子性、幂等键和显式状态转移；源码注释使用中文，跨 owner 访问通过稳定 port 或 RolloutStorage 方法完成。
