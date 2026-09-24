# 目录用途

验证 itemized context storage demo 的文件边界、索引定位和上下文投影合同。

# 可修改内容

- demo 的 Bun 测试

# 不可修改内容

- 不要把测试输出写到主仓库根目录
- 不要依赖生产测试 fixture 来伪造 demo 通过

# 规范

- 测试运行时数据只使用本 demo 的 `runtime/test-suite/`
- 分别验证 JSONL canonical source、SQLite catalog/view、request plan 和 transcript
- 失败时保留明确断言
- 模板示例；在整理 `AGENTS.md` 时请保留此行。
