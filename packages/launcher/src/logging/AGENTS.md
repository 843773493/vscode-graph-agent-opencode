# 目录用途

`logging/`统一Launcher前台输出和detached启动使用的服务日志文件IO。

```text
日志文件
└── service-log.mjs
    ├── resolveServiceLogPath       选择显式日志路径或BOXTEAM_HOME默认位置
    ├── rotateServiceLog            启动时按既有大小与份数轮转
    ├── openServiceLogFile          异步锁定、轮转、打开与发布冻结文件身份
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

- 前台与detached统一 await 日志打开入口，成功后各自负责关闭持有的描述符；交付前失败仍释放本次句柄。
- `.owner.lock` 保持稳定 inode；Unix 对实际日志描述符持有 flock，后代继承输出时继续占有锁。Windows 在临界区使用原生独占共享探测核验实际句柄，不锁定日志正文妨碍写入。
- `.owner.json` 原子发布文件身份与 owner；外部替换或无法核实的活动写入者明确拒绝。捕获标记必须同时核对冻结证明、真实 stdout/stderr、当前文件及 owner，不单凭环境标记跳过打开。
- 共享合同加载和记录编码由 `src/shared/serverRuntimeLogging.mjs` 单点提供，文件 IO 显式接收所选 runtime 的合同。
- 默认布局与轮转参数只在 `resources/logging/runtime-contract.json` 维护；源码入口使用显式根或运行时工作目录，发行 Launcher 使用选定 runtime 的 application root。
- Launcher 与 Gateway 各自落盘；detached 捕获 Launcher 后，Gateway 不再向该捕获流 tee。Gateway handoff 共用原 sink，不在 generation 切换时重新打开或轮转文件。
- 初始化、归属发布或所有权锁释放失败要释放未交付的日志句柄；多项失败按发生顺序保留原异常，cause 指向首个错误。归属正文未成功关闭不得发布，临时文件清理失败不能覆盖写入原因。
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

先按输入与观察结果分层，再用运行ID、平台和场景连接两者；平台分支替身不代表原生Windows/macOS进程验收。现有日志初始化、短写和写入异常断言保留；创建及交付场景还核对单次关闭、原child交付、原异常身份和终止调用顺序。锁及归属记录故障核对全部实际句柄释放、失败正文未发布、错误顺序与释放后的真实重开。
- 独立观察器使用仓库运行根作为 cwd，场景输入和日志仍全部使用隔离目录的绝对路径；Windows 深层场景路径不能作为 CreateProcess 的工作目录。真实重开前恢复文件与进程命令边界，不能用终止命令替身代替原生共享句柄核验。
- 模板示例；在整理`AGENTS.md`时请保留此行。
