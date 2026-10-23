# 目录用途

本目录承载 Linux 与 Windows 自包含 runtime 的安装布局、relocation、启动及产品验收入口，并提供安装验证共享的 Gateway 就绪探针和相应测试。

```text
verification/
├── verify-{linux,windows}-x64.mjs       各平台安装、启动及关闭流程
├── verification-readiness.mjs          共用Gateway就绪观察
├── verification-readiness.test.mjs     就绪HTTP与进程状态场景
├── verification-shutdown.test.mjs      Windows Gateway停止观察
└── verification-shutdown-timers.test.mjs  两验证器的进程终态与关闭等待
```

先按安装流程、就绪观察、关闭观察区分职责，再进入相应场景。关闭清理以`exitCode`或`signalCode`判断Launcher是否已结束；信号退出不是仍在运行，不再发送信号或创建45秒等待。活跃进程保留原信号、等待期限和超时强杀行为。

关闭计时器测试的独立Node观察将可重放的`observer.mjs`、`input.json`保留在`out/tests/packaging/runtime/verification/verification-shutdown-timers/workspace/<run_id>/<scenario>/`，对应原始结果位于同根`artifacts/<run_id>/<scenario>/`。信号终态场景只终止自己创建的Node子进程，再调用真实生产函数；后续发信号边界由替身拦截，不能触及已复用PID或用户进程。Linux实测与Windows/macOS平台替身的证据范围分开，不能把后者表述为原生验证。

# 可修改内容

- `verify-linux-x64.mjs`、`verify-windows-x64.mjs` 两个平台的安装验收流程。
- `verification-readiness.mjs` 及其安装验证、Launcher 关闭测试。

# 不可修改内容

- 不在这里实现 runtime staging、归档下载、版本清单或构建输入指纹；这些职责留在父目录。
- 不保留旧位置的转发脚本、兼容入口或第二套安装验证实现。
- 不在测试中连接用户服务或写入构建产物目录。

# 规范

- 发布包装器负责按平台先构建、再调用本目录对应的 verifier；跨平台 Windows 验证入口也调用 `verify-windows-x64.mjs`。
- 两个平台共用 Gateway 就绪探针，平台特有的安装、Launcher 关闭和产品验收仍归各自 verifier。
- 测试运行文件写入 `out/tests/packaging/runtime/verification/<测试名>/workspace/` 镜像目录。
- 就绪和停止观察测试按 `workspace/<run_id>/<scenario>/` 保留每次独立运行的fixture，并在同镜像根 `artifacts/<run_id>/<scenario>/` 独占保存原始 `stdout.raw`、`stderr.raw` 和精简 `result.json`；不得清理或覆盖这些结果。观察函数仍须清理本次HTTP连接、socket和timer。
- JavaScript 使用 ESM。生产文件纳入 runtime 构建输入指纹；本目录的 `AGENTS.md` 与 `*.test.mjs` 按父目录规则排除。
- 模板示例；在整理 `AGENTS.md` 时请保留此行。
