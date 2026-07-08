# 目录用途

`Ssh_text_attach/` 是一个跨端 txt 文件 attach 演示项目。宿主机前端连接宿主机后端，后端可直接管理本地 txt 文件，也可通过 SSH 隧道连接容器内同款后端并编辑容器侧 txt 文件。

# 可修改内容

- demo 的前后端源码、测试、脚本和样式
- demo 自己的 Dockerfile、Compose 配置和 SSH 演示密钥
- demo 的 attach 目标配置

# 不可修改内容

- 不要修改主应用 `app/`、`src/` 的生产代码
- 不要把此 demo 的依赖提升到仓库根目录
- 不要在 `.boxteam/` 之外写入运行时 txt 数据

# 规范

- JavaScript 使用 ESM
- 服务失败要直接暴露具体错误
- 本地与远程保存都必须以后端返回的完整文件快照为准
- 模板示例；在整理 `AGENTS.md` 时请保留此行。
