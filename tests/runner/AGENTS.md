# 目录用途

存放测试套件注册、能力矩阵和统一命令入口，不包含具体测试实现。

## 可修改内容

- `matrix.jsonc`、ESM runner、套件过滤和结构化汇总。

## 不可修改内容

- 不要隐藏失败、吞掉退出码或把未满足前置条件记为通过。
- 不要让 E2E 套件回退执行 Integration stub 套件。

## 规范

- `integration-runtime-context-publication` 通过真实 Session、模型准备与公开 fork 入口验证仅发布事件不创建 Job/Turn、已发送请求不变、下一次准备消费及 fork 锚点闭包；提交前/后故障经全新 Saver/dispatcher 核对冷 owner 恢复。复用 model_stream 和正式 fork watchdog，固定240秒/4GiB，不代表持久任务完整运行时、进程重启或主 Web 验收。

- `integration-web-mcp-authorization` 通过真实 Chromium、Gateway、Session/Agent、同一 OAuth/MCP 资产及 model_stream 验证会话授权入口、私有弹窗回调、工作区底部标签持久化、完整定义和真实调用；固定240秒／4GiB，不代替远程 Gateway、真实 Provider 或完整后台等待恢复。

- `integration-tool-discovery-policy` 通过真实 Gateway、Agent 和 HTTP MCP 核对首次发布前已禁用目标的身份、硬隐藏、配对拒绝、远端零执行及封存 request_hash，固定160秒／4GiB。

- `integration-mcp-connection-wait` 经真实 Gateway、Agent、OAuth/MCP 与 model_stream 验证原配对等待、无新用户 Turn 的独立 continuation、两种激活边界及 mailbox／dispatcher 崩溃窗口，固定600秒／4GiB；不替代 Web 授权交互或真实 Provider 验收。

- `integration-mcp-connection-control` 经真实 Gateway、Backend、Session 消息及 Agent 发现／信封入口，验证首次 OAuth 未授权时的本地控制、兄弟 Server 调用、受信 operation 身份、短 status、参数拒绝与 disconnect，固定240秒／4GiB；模型复用 model_stream，不替代授权 UI、长等待自动继续或真实 Provider 验收。
- `integration-mcp-oauth-startup` 使用真实 HTTP OAuth／MCP 与真实 Backend 启动，核对首次需授权的局部状态、兄弟 Server、同 request_id、SDK PKCE／state、冷刷新和敏感信息边界，固定90秒／4GiB；不替代产品 connect、授权UI、持久等待或Gateway／Web验收。
- `integration-mcp-stdio-recovery` 使用真实 Backend、单实例 OS 锁与 SDK stdio，覆盖 POSIX 非合作进程强制关闭、关闭期限失败及旧实例停止后的 OS 退出／claim 冷恢复，固定180秒／4GiB；不替代 Windows Job Object、OAuth、HITL 或 Web 验收。
- `integration-mcp-stdio-replacement` 使用真实单实例锁、stdio SDK、watcher、Runner和model_stream，核对停止意图先于关闭、停后再启、破坏性失败与同入口修复及兄弟工作区隔离，固定180秒／4GiB；不代替非合作进程终止、退役清理诊断、OAuth或Web验收。
- `integration-mcp-config-recovery` 通过真实 Backend OS 退出验证 MCP promotion 前后、立即／lease 到期重启、原 claim fencing、连接重建和真实 HTTP SSE 重放，固定240秒／4GiB；不代替 stdio 停后再启、OAuth或Web验收。
- `integration-mcp-config-replacement` 通过真实双挂载、配置 watcher、Runner、model_stream 和 HTTP MCP Server 核对旧 Turn／新 Turn 连接归属、部分失败、混合待重启及工作区隔离，固定180秒／4GiB；不替代进程崩溃恢复、OAuth或Gateway／主Web验收。
- `integration-extension-discovery` 复用正式 fork watchdog，覆盖 Owner 发布、冻结完整定义、搜索／分页／策略、实际输出中间件、冷恢复、真实 stdio 信封调用及 stdio／HTTP 进度与原请求取消，固定 180 秒/4GiB；不替代完整 Agent、授权或主 Web 验收。

- 所有 JavaScript 使用 ESM 并通过 Bun 执行。
- 矩阵显式声明层级、客户端、运行面、命令和真实前置条件。
- `unit-workspace-services` 递归发现 Browser、Terminal 的服务端、attach 页面纯逻辑及协议 JS 单测；它不启动实际辅助服务，不能代替真实进程 Integration 验收。
- `run-workspace-services.mjs` 按静态导入的测试框架分组：`node:test` 交给Node原生测试器，`bun:test` 交给Bun。两组都执行并汇总失败，未知或混用框架在启动前拒绝；各进程沿用矩阵的超时与内存保护。测试器的编码等运行时语义须与被测行为匹配，不能用Bun的Node兼容行为代替Node服务端证据。
- `integration-browser-element-references` 使用真实Chromium验证辅助Browser服务的DOM引用与动作，固定60秒/4GiB；不代表Gateway或主Web产品链路验收。
- `integration-terminal-shutdown-signals`通过真实HTTP/PTY及POSIX信号核对在途创建与关闭重试，显式就绪交付屏障保留原Worker结果，固定90秒/4GiB；不代表Windows、Gateway/主Web或整个shutdown总期限。
- `integration-browser-checkpoint-form-state` 使用真实Chromium与HTTP表单验证检查点捕获、DOM变更后的定位、释放后恢复及大型匿名表单的扫描成本，固定60秒/4GiB；Manager落盘边界为空操作，不代表持久化或主Web验收。
- `integration-persistent-task-schedules`经任务API、受控时钟、model_stream和真实Backend硬重启，核对interval/cron、时区、补跑、暂停恢复及原计划身份，固定240秒/4GiB；不替代脚本/模型条件、Goal/生成器迁移或主Web验收。
- `integration-persistent-tasks-manual`通过任务 API、原 source owner 与 model_stream 核对手动触发、试运行、忙时排队、revision 以及接受后硬重启对账，固定300秒/4GiB；不代表定时、脚本/模型条件、Goal/生成器迁移或主Web验收。
- 每个已启用 suite 都带数据段内存上限与硬超时；被上限或超时中止的套件必须以 TIMEOUT 或非零退出码报出，不得记成通过。
- `integration-resource-operation-admission`经正式创建及原工具operation验证真实PTY/Chromium资源锁等待到期和显式重试，固定90秒/4GiB；不代表整个工具执行、Gateway/Web或Backend重启验收。
- `integration-resource-thread-admission`经正式资源入口和导航worker验证短Session准入、同Session独立资源并行及原holder退出后的删除排空，固定90秒/4GiB；不代表整个工具operation的截止或终止核实。
- `integration-context-source-epoch-copy`复用正式fork watchdog，核待消费、已消费和继续执行后的来源版本、epoch transition与封存前缀复制，固定120秒/4GiB；不替代完整上下文或Web验收。
- `integration-context-source-wire`核来源封装经过同步/异步真实HTTP、跨协议fallback、ToolSet切换及冷fork/后继fork时的正文、引用与hash，固定240秒/4GiB并复用正式fork watchdog；外部模型使用现有协议资产，不代表真实Provider或Web验收。
- POSIX套件在独立进程组中运行，根进程退出后仍回收组内后代；覆盖正常、失败、超时和中断路径。Windows suite 通过 `tests/support/processes/windows_job.py` 在独立 Job Object 中启动，挂起根进程直到完成绑定，覆盖整个进程树的内存上限，并在根进程退出或监督器关闭时回收后代；Windows开发目标测试也复用这套实现。
- 首次SIGINT/SIGTERM请求终止套件，保留30秒升级宽限；再次中断立即强杀，原套件强杀期限不重置。POSIX按信号终止进程组；Windows用 `taskkill /T` 终止监督器进程树，并在升级阶段加 `/F`。等待监督器退出后才返回，中断仍为130，超时仍为3及原TIMEOUT回执。

- `integration-extension-definition-recovery` 通过真实 Runner 发现、压缩和 fork，使用正式 fork watchdog；不代表外部 Provider 或 Web 验收。
