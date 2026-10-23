# 目录用途

`scripts/` 存放 Itemized Context Storage demo 的构建和源码检查脚本。

# 可修改内容

- 不运行 demo、不产生持久文件的静态检查脚本

# 不可修改内容

- 不要在这里实现存储、HTTP 服务或浏览器业务逻辑
- 不要把测试运行时数据写入 demo 的 `runtime/`

# 规范

- 使用 ESM 和 Bun API
- 构建检查使用 `write: false`，避免生成检查产物
- 模板示例；在整理 `AGENTS.md` 时请保留此行。
