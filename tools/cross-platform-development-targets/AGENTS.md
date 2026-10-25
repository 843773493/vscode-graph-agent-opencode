# 目录用途

本目录存放跨平台独立开发目标的公共配置、schema、宿主机编排模块和平台工具。

```text
cross-platform-development-targets/
├── 配置契约
│   ├── target.schema.json       目标配置的结构与字段约束
│   ├── targets.example.jsonc    不含真实凭据的配置示例
│   ├── config.mjs               展开路径、校验并选择目标
│   └── cli-options.mjs          解析统一入口的命令行参数
├── 宿主编排
│   ├── process.mjs              统一运行外部命令并保留失败信息
│   ├── snapshot.mjs             创建待同步的 Git 源码快照
│   ├── ssh-target.mjs            通过 SSH 执行目标动作、传输和校验
│   ├── windows-git-ssh.mjs       适配 Windows Git 调用 OpenSSH
│   └── docker-target.mjs         创建和管理本地 Docker Linux 目标
├── linux/                       Linux 目标的 POSIX 管理动作
├── windows/                     Windows 目标的 PowerShell 管理动作
└── docker/                       提供 SSH 目标环境的镜像与持久化入口
```

命令从 `scripts/launch/cross-platform-development-target.mjs` 进入：配置层解析目标，快照层准备源码，SSH 层调用对应平台动作；Docker 只负责提供 Linux SSH 主机。平台实现留在各自目录，公共编排不拼接平台专属生命周期逻辑。

# 可修改内容

- 可以维护目标配置 schema、示例、ESM 编排模块及 Linux、Windows、Docker 平台工具。
- 可以增加不包含真实凭据的目标示例和测试辅助代码。

# 不可修改内容

- 不得提交真实 SSH 私钥、密码、`.env` 内容或实际 VMware 地址。
- 不得在这里实现 Gateway 或工作区后端业务逻辑。

# 规范

- 宿主机 JavaScript 始终使用 ESM，并从运行时工作目录或显式项目根目录解析路径。
- 平台脚本快速失败并输出目标、平台和失败阶段，不得伪造成功。
- 配置中的 SSH host key 校验不得被关闭。
- 模板示例；在整理 `AGENTS.md` 时请保留此行。
