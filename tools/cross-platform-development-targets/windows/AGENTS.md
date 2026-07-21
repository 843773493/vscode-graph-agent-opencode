# 目录用途

本目录存放 Windows 开发目标通过 OpenSSH 执行的 PowerShell 管理脚本。

# 可修改内容

- 可以维护 Windows 路径、`.venv`、进程、ACL、服务状态和产物收集动作。

# 不可修改内容

- 不得调用 `sh`、`nohup`、`kill` 等 POSIX 专属命令。
- 不得假设 VMware 地址、Windows 用户名或盘符。

# 规范

- 使用严格错误模式并通过结构化动作参数分派。
- TODO 必须明确标注尚需真实 Windows VMware 验证的兼容性代码。
- 模板示例；在整理 `AGENTS.md` 时请保留此行。
