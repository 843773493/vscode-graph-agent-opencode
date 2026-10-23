# 目录用途

验证 itemized context storage demo 的文件边界、索引定位和上下文投影合同。

```text
tests/
└── storage.test.js  JSONL/SQLite 合同、初始化失败重试与未登记尾部拒绝、catalog/记录身份校验、request plan、transcript、runtime 路径边界
```

# 可修改内容

- demo 的 Bun 测试

# 不可修改内容

- 不要把测试输出写到主仓库根目录
- 不要依赖生产测试 fixture 来伪造 demo 通过

# 规范

- 测试运行时数据写入 `out/tests/examples/demos/Itemized_context_storage/tests/storage.test/workspace/<run_id>/runtime/`
- `afterEach` 关闭所有 SQLite 连接并保留本轮运行目录，便于失败复查
- 分别验证 JSONL canonical source、SQLite catalog/view、request plan 和 transcript
- runtime 相对路径同时接受 `/` 与 `\\` 分隔符；reset 测试用同级 marker 验证不误删相邻数据
- 失败时保留明确断言
- 模板示例；在整理 `AGENTS.md` 时请保留此行。
