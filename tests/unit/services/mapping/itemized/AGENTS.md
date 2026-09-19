# 目录用途

镜像 `app/services/mapping/itemized/`，存放 v2 selection 到 LangChain/history DTO 纯映射的单元测试。

## 可修改内容

- itemized 映射与去重规则的纯映射单元测试。

## 不可修改内容

- 不放需要真实 RolloutStorage、SQLite/JSONL 的测试（属于 rollout_context 集成合同）。

## 规范

- 只构造内存中的 `CanonicalItemRecord`，不做任何 I/O；临时状态一律 `tmp_path`。
