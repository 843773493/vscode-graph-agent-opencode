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
- `runtime-manifest.json` 的 `skill_resources` 指向发行包内 `application/resources/skills`；Launcher 只校验并传递资源根目录，不实现 Skill 或 Agent 逻辑。
- npm 版本使用 Launcher Node；Windows 便携版使用 runtime manifest 声明的 bundled Node。
- 模板示例；在整理 `AGENTS.md` 时请保留此行。
