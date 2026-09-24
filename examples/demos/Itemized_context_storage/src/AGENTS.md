# 目录用途

存放 itemized context demo 的存储核心、CLI 和本地 HTTP 服务。

# 可修改内容

- demo 存储模型和演示服务

# 不可修改内容

- 不要依赖主仓库生产模块
- 不要把运行时状态写到 `runtime/` 之外

# 规范

- 使用 ESM 和 Bun 内置能力
- 存储边界要在代码中保持可读
- 模型 context、transcript 和 request-only 数据必须明确区分
- 模板示例；在整理 `AGENTS.md` 时请保留此行。
