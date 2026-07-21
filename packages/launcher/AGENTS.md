# 目录用途

`packages/launcher/` 实现用户可执行的 `boxteam` 本地启动与控制命令，不是终端对话式 Agent。

# 可修改内容

- 可以维护命令入口、runtime manifest、Gateway 前台监督、实例锁和诊断。

# 不可修改内容

- 不实现 Agent、Job、会话或工作区业务规则。
- 不猜测源码仓库根目录，不回退到 PATH 中的 Python。

# 规范

- JavaScript 始终使用 ESM。
- 所有安装资源通过显式 runtime manifest 解析。
- npm 版本使用 Launcher Node；未来 bundled Node 只保留明确扩展点。
- 模板示例；在整理 `AGENTS.md` 时请保留此行。
