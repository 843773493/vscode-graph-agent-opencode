# 目录用途

存放测试套件注册、能力矩阵和统一命令入口，不包含具体测试实现。

## 可修改内容

- `matrix.jsonc`、ESM runner、套件过滤和结构化汇总。

## 不可修改内容

- 不要隐藏失败、吞掉退出码或把未满足前置条件记为通过。
- 不要让 E2E 套件回退执行 Integration stub 套件。

## 规范

- 所有 JavaScript 使用 ESM 并通过 Bun 执行。
- 矩阵显式声明层级、客户端、运行面、命令和真实前置条件。
- `unit-workspace-services` 递归发现 Browser、Terminal 的服务端、attach 页面纯逻辑及协议 JS 单测；它不启动实际辅助服务，不能代替真实进程 Integration 验收。
- `run-workspace-services.mjs` 按静态导入的测试框架分组：`node:test` 交给Node原生测试器，`bun:test` 交给Bun。两组都执行并汇总失败，未知或混用框架在启动前拒绝；各进程沿用矩阵的超时与内存保护。测试器的编码等运行时语义须与被测行为匹配，不能用Bun的Node兼容行为代替Node服务端证据。
- `integration-browser-element-references` 使用真实Chromium验证辅助Browser服务的DOM引用与动作，固定60秒/4GiB；不代表Gateway或主Web产品链路验收。
- `integration-terminal-shutdown-signals`通过真实HTTP/PTY及POSIX信号核对在途创建与关闭重试，显式就绪交付屏障保留原Worker结果，固定90秒/4GiB；不代表Windows、Gateway/主Web或整个shutdown总期限。
- `integration-browser-checkpoint-form-state` 使用真实Chromium与HTTP表单验证检查点捕获、DOM变更后的定位、释放后恢复及大型匿名表单的扫描成本，固定60秒/4GiB；Manager落盘边界为空操作，不代表持久化或主Web验收。
- 每个已启用 suite 都带数据段内存上限与硬超时；被上限或超时中止的套件必须以 TIMEOUT 或非零退出码报出，不得记成通过。
- `integration-resource-operation-admission`经正式创建及原工具operation验证真实PTY/Chromium资源锁等待到期和显式重试，固定90秒/4GiB；不代表整个工具执行、Gateway/Web或Backend重启验收。
- `integration-resource-thread-admission`经正式资源入口和导航worker验证短Session准入、同Session独立资源并行及原holder退出后的删除排空，固定90秒/4GiB；不代表整个工具operation的截止或终止核实。
- `integration-context-source-epoch-copy`复用正式fork watchdog，核待消费、已消费和继续执行后的来源版本、epoch transition与封存前缀复制，固定120秒/4GiB；不替代完整上下文或Web验收。
- `integration-context-source-wire`核来源封装经过同步/异步真实HTTP、跨协议fallback、ToolSet切换及冷fork/后继fork时的正文、引用与hash，固定240秒/4GiB并复用正式fork watchdog；外部模型使用现有协议资产，不代表真实Provider或Web验收。
- POSIX套件在独立进程组中运行，根进程退出后仍回收组内后代；覆盖正常、失败、超时和中断路径。Windows suite 通过 `tests/support/processes/windows_job.py` 在独立 Job Object 中启动，挂起根进程直到完成绑定，覆盖整个进程树的内存上限，并在根进程退出或监督器关闭时回收后代；Windows开发目标测试也复用这套实现。
- 首次SIGINT/SIGTERM请求终止套件，保留30秒升级宽限；再次中断立即强杀，原套件强杀期限不重置。POSIX按信号终止进程组；Windows用 `taskkill /T` 终止监督器进程树，并在升级阶段加 `/F`。等待监督器退出后才返回，中断仍为130，超时仍为3及原TIMEOUT回执。
