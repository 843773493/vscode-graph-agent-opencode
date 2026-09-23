# 目录用途

存放 `app/services/infrastructure/rollout_context/` 的单元测试，覆盖 checkpoint Saver、compaction 边界、canonical itemized domain、context source manager/control state、stream accumulator、seal/dispatch preflight 与 fork 边界等模块的合同行为。

## 可修改内容

- item/commit/recovery、seal、tool protocol closure、fork、mutation intent 与 context source 生命周期的单元级合同测试。
- 以真实 SQLite/JSONL 会话 bundle 固化行为的 fixture 与失败注入断言。

## 不可修改内容

- 生产 rollout_context 实现、canonical domain 与存储 schema。
- 不得恢复旧 rollout shim，不得伪造被测持久化结果，也不得把本地协议桩或回放结果当作真实 Provider E2E。
- `asset/` 只读模板不得作为输出目录。

## 规范

- 依赖注入统一用 pytest fixture（如 `tmp_path`、`session_bundle_factory`）构造隔离会话 bundle；应用代码统一用 FastAPI Depends。
- 真实 SQLite/JSONL 必须落在 `tmp_path` 或同名正式测试输出，不得写入项目根或产生根级 `.boxteam/`。
- 失败路径必须同时断言明确错误与已提交数据未被错误改写；不得通过放宽断言或 `skip` 掩盖真实缺陷（程序绝不能默默失败）。
- 断言以 canonical domain 与封存 typed 字段为准，不得依赖 extensions/metadata 中的同名控制 key 或内部实现细节。
