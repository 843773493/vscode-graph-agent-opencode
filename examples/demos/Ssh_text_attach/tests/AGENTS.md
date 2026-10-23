# 目录用途

`tests/` 存放 Ssh_text_attach demo 的 Bun 单元测试。

```text
tests/
├── clientMain.test.js 浏览器入口 fixture：保存/刷新期间保留草稿、输入时复用目标列表
├── config.test.js      Gateway 目标配置和端口解析
├── dockerAccess.test.js Docker CLI 与 Linux sudo 回退选择及失败诊断
├── fileStore.test.js   本地 txt 文件创建、快照、保存顺序与并发请求
├── processOutput.test.js 日志片段上限、跨 chunk UTF-8 边界和输出背压
├── processTree.test.js POSIX TERM→KILL 超时及真实 detached 进程组/后代回收、Windows taskkill 进程树回收
├── sshCommand.test.js  SSH 命令参数和跨平台 known-hosts 空设备路径
├── staticFiles.test.js 静态资源读取、路径越界拒绝和前端运行配置
├── tunnel.test.js      SSH stderr 跨 chunk UTF-8、健康轮询、响应体释放和子进程关闭确认
├── upstreamProxy.node-smoke.mjs Node 原生 HTTP 验证请求流式、断连清理、分块背压超限和逐跳头过滤
└── upstreamProxy.test.js 请求体限制、逐跳头和 Node 原生代理 smoke
```

# 可修改内容

- 配置解析测试
- 浏览器入口 attach、刷新、保存与编辑状态测试
- 文件存储测试
- 静态资源服务测试
- 上游代理请求体流式、大小上限和断连清理测试
- SSH 隧道健康检查测试
- 启动器的 POSIX 限时升级、退出确认和 Windows 进程树清理测试
- 启动器日志输出的分段和背压测试
- 后续可增加 SSH attach 相关测试

# 不可修改内容

- 不要依赖真实用户 SSH 环境
- 不要写入 demo `.boxteam/` 之外的持久运行时数据

# 规范

- 运行 Bun 测试必须使用仓库根目录 `AGENTS.md` 规定的进程外超时和内存保护；禁止裸跑 `bun test`
- 文件存储每个用例都将仓库根目录 `tests/fixtures/workspaces/default_test_workspace` 独立复制到 `out/tests/examples/demos/Ssh_text_attach/tests/fileStore.test/workspace/<run_id>/<test_case>/` 后再修改；保留运行副本供复查，不能改 fixture 源目录
- 测试失败必须暴露具体断言或错误
- 模板示例；在整理 `AGENTS.md` 时请保留此行。
