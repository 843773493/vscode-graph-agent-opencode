# 目录用途

`logging/`统一Launcher前台输出和detached启动使用的服务日志文件IO。

```text
日志文件
└── service-log.mjs
    ├── resolveServiceLogPath       选择显式日志路径或BOXTEAM_HOME默认位置
    ├── rotateServiceLog            启动时按既有大小与份数轮转
    ├── openServiceLogDescriptor    创建、轮转、打开与初始化权限
    │   ├── openServiceLog          持久日志与前台输出的tee，持有描述符
    │   └── ../detached-process.mjs 将描述符交给子进程stdio后关闭父端
    └── 测试
        ├── service-log.test.mjs        正常写入、外层接管与轮转
        └── service-log-errors.test.mjs 短写、初始化及清理失败
```

# 可修改内容

- 日志路径、轮转、文件描述符初始化、同步写入与相关测试。
- 前台和detached消费者的日志文件接线。

# 不可修改内容

- 不在这里实现Gateway、工作区或Agent业务，不管理业务执行状态。
- 不在这里保存运行日志，不为日志失败伪造成功或回滚结果。

# 规范

- 前台与detached共用日志描述符初始化owner，成功后各自负责关闭持有的描述符。
- 共享合同加载和记录编码由 `src/shared/serverRuntimeLogging.mjs` 单点提供，文件 IO 显式接收所选 runtime 的合同。
- 默认布局与轮转参数只在 `resources/logging/runtime-contract.json` 维护；源码入口使用显式根或运行时工作目录，发行 Launcher 使用选定 runtime 的 application root。
- Launcher 与 Gateway 各自落盘；detached 捕获 Launcher 后，Gateway 不再向该捕获流 tee。Gateway handoff 共用原 sink，不在 generation 切换时重新打开或轮转文件。
- 初始化失败要尝试释放；释放也失败时按发生顺序保留两份原异常，cause指向初始化错误。
- 同步日志写入必须处理短写，完整落盘调用成功后才交前台；这不表示fsync持久化，部分写入后失败也不声称已回滚。
- Windows沿用继承ACL，不把POSIX mode bits称为Windows权限验收。
- 日志描述符由持有者负责关闭；故障注入只在独立Node中修改文件、创建和终止边界，不向宿主PID发送信号、不启动后台进程。
- `service-log-errors.test.mjs`的输入与原始结果按`out/tests/packages/launcher/src/logging/service-log-errors/{workspace,artifacts}/<run_id>/<platform>/<scenario>/`独占留存；不在结束时删除现场，每次运行只保存一份共用的`observer.mjs`。每场景先保存input、Node原始stdout/stderr、准确命令和实际退出原因，再判断或解析。

```text
service-log-errors/
├── workspace/<run_id>/<platform>/<scenario>/中文 日志.log  本次真实日志IO现场
└── artifacts/<run_id>/
    ├── observer.mjs                   共用的独立Node入口
    └── <platform>/<scenario>/         input.json、stdout.raw、stderr.raw、result.json
```

先按输入与观察结果分层，再用运行ID、平台和场景连接两者；平台分支替身不代表原生Windows/macOS进程验收。现有日志初始化、短写和写入异常断言保留；创建及交付场景还核对单次关闭、原child交付、原异常身份和终止调用顺序。
- 模板示例；在整理`AGENTS.md`时请保留此行。
