# 目录用途

提供 itemized context storage demo 的只读浏览器展示页面。

# 可修改内容

- 页面结构、样式和状态展示脚本

# 不可修改内容

- 不要在浏览器端直接写 SQLite 或 canonical JSONL
- 不要把本地状态伪造为后端存储成功

# 规范

- 页面只读取 `/api/state`
- 关键存储字段必须展示真实返回数据
- JavaScript 使用 ESM
- 模板示例；在整理 `AGENTS.md` 时请保留此行。
