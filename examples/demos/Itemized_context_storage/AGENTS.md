# 目录用途

`Itemized_context_storage/` 是当前主仓库 item 化模型上下文的独立测试与教学 demo，展示 canonical item、JSONL、SQLite catalog/view、request-only 计划和 transcript 投影之间的边界。

# 可修改内容

- demo 自己的 Bun 源码、网页、测试和说明
- `runtime/` 下由 demo 产生的运行时数据
- demo 自己的 `package.json` 和锁文件

# 不可修改内容

- 不要导入或修改主仓库 `app/`、`src/` 的生产运行时
- 不要把运行时数据写到 demo 目录之外
- 不要把 `Message` 或 transcript 投影当作 canonical item 的存储源

# 规范

- JavaScript 使用 ESM，通过 Bun 运行
- `runtime/` 是唯一运行时根目录，清理操作只能作用于该 demo 自己的子目录
- JSONL 保存 canonical item 正文；SQLite 只保存索引、投影和 view 状态
- 失败时暴露具体错误，不能静默降级
- 模板示例；在整理 `AGENTS.md` 时请保留此行。
