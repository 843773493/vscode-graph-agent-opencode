# 目录用途

`scripts/` 存放 demo 的开发检查脚本。

```text
scripts/
├── roles-up.mjs  从 demo 根目录启动宿主机/容器角色；按平台回收进程树并限制日志行缓冲
├── docker-up.mjs 从 demo 根目录执行 Docker/Compose CLI
├── dockerAccess.js 按宿主平台选择 Docker CLI 或 Linux 无交互 sudo 前缀，并保留检查诊断
├── processTree.js 跨平台终止角色子进程树；POSIX 按信号阶段限时等待进程组退出并升级信号，Windows 限时执行 taskkill `/T`
├── processOutput.js 分段转发角色日志并按目标背压暂停来源流
└── check.mjs     用 Bun 解析器检查源码语法与 JSON 配置
```

# 可修改内容

- 静态检查脚本
- demo 本地开发辅助脚本

# 不可修改内容

- 不要把长命令塞回 `package.json`
- 不要依赖仓库根目录的运行时状态

# 规范

- 脚本使用 ESM
- 运行根目录从脚本自身位置解析；启动子进程时显式传入 `cwd`，不依赖调用者当前目录
- 检查脚本从自身位置定位 demo 根目录，并递归语法检查 `src/`、`scripts/`、`tests/` 下的 JavaScript 文件
- 检查失败时返回非零退出码
- POSIX 服务进程必须独占进程组；关闭时整组先收 SIGTERM，5 秒内未退出则发 SIGKILL，再限时确认进程组清空；Windows 通过 taskkill `/T` 回收进程树，非零退出或 10 秒超时都必须报错，直接子进程退出不能代替后代进程回收确认
- 子进程日志按有界片段转发，并按 stdout/stderr 背压暂停来源流；不得因超长无换行输出或慢速日志接收端持续占用内存
- 模板示例；在整理 `AGENTS.md` 时请保留此行。
