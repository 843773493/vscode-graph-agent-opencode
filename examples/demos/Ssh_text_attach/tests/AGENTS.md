# 目录用途

`tests/` 存放 Ssh_text_attach demo 的 Bun 单元测试。

# 可修改内容

- 配置解析测试
- 文件存储测试
- 后续可增加 SSH attach 相关测试

# 不可修改内容

- 不要依赖真实用户 SSH 环境
- 不要写入 demo `.boxteam/` 之外的持久运行时数据

# 规范

- 使用 `bun test`
- 测试失败必须暴露具体断言或错误
- 模板示例；在整理 `AGENTS.md` 时请保留此行。
