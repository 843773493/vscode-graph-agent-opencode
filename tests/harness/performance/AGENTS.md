# 目录用途

正式测试的性能记录、进程外资源采样、pytest 阶段观察及报告比较。沿用现有 runner 和 TestRunContext，不管理产品服务。

# 可修改内容

- 轻量记录、按需观察、诊断及相关产物格式。

# 不可修改内容

- 产品逻辑、业务性能预算、原进程保护和既有测试结果。

# 规范

- 默认只采集标量，不记录 SQL 参数、正文、凭据或完整环境。缺失不填零。
- 阶段 inclusive，进程资源不冒充操作独占成本；跨时钟域不直接相减。
- 所有验证走矩阵或外部超时/内存保护。正式产物镜像 tests 路径，新增机制必须有实际调用方。

## Agent 排查入口

优先只调用一次 `bun run test:matrix -- --suite=integration-performance-core`：真实接受入口造两种历史规模，报告快照读取、完整初始化、造数、闸门等待、模型回放及资源成本。先根据终端摘要归因；仅在需要原始样本时再调用 `uv run --no-sync python -m tests.harness.performance.report show <性能运行目录> --stage rollout.snapshot_read --json`。不为已有阶段重写探针，不为取更好数字重跑。

- 普通 suite 自动 basic；`--perf=core` 开启已有核心观察。浏览器使用 `bun run test:matrix -- --suite=e2e-web-interactive-latency --perf=core`，发送/流式输出使用已有 `integration-web-chat-tool-loop`。共享构建、闸门等待不等于业务耗时，原预算不放宽。
- 运行开始即输出目录。运行中 `report show <目录>` 可看未完成阶段和采样峰值；完成后读取同目录 `performance-summary.json`。`--stage <子串>` 缩小输出，`--json` 获取完整记录索引、样本和指标。
- Agent列表等页面初始化API已由浏览器请求完成事件观察，使用 `report show <目录> --stage /api/v1/agents` 即可筛选列表，追加 `--json` 查看请求身份和响应头/完整消费事件区间；不为列表重复创建SDK或发送模型请求。浏览器请求样本不等价于首次导入SDK的冷启动测量。
- 明确需要深入时，矩阵追加 `--perf=diagnostic --perf-tool=cpu`；tool也可选 `allocations`、`browser-cpu`、`browser-trace`，每次一种。Python工具用于pytest call；browser工具用于已接入的Chromium页面。未执行所请求工具会报告基础设施失败；trace只保留无正文时序元数据。
- 比较显式执行 `uv run --no-sync python -m tests.harness.performance.report compare <基线performance-summary.json> <候选performance-summary.json>`。对照先核workload/冷热/并发/采集档位与环境；报告不自动改阈值或基线。少样本分位数只作描述。
- 直接pytest支持 `--perf` 和显式 `--perf-profile=cpu`（或 `allocations`），只提供阶段/诊断，不自行启动监督器；仍须外部超时和内存保护。优先正式矩阵。
- 资源统一由psutil采样：默认250ms取已知进程资源，后代发现间隔至少5秒，Windows取句柄、macOS/Linux取文件描述符；平台不支持和短进程覆盖缺口明确记录，不补零。短suite的采集进程启动及首次后代发现成本单列，不能视为零开销。
- 固定轮次可调用 `checkpoint_resources(perf, label, round)`；仅在 core/diagnostic 采当前进程及后代的 RSS/句柄/线程，不强制GC或自动判定泄漏。SQLite聚合只覆盖默认Connection，定制factory跳过数量明确报告，保留其原执行/cursor语义；消费行数不等于扫描行数。锁/连接池等待、缓存命中率和SSE渲染积压只在已有明确观察边界时报告，不从耗时猜测。
- 当前原生运行证据来自Linux；Windows/macOS使用同一psutil实现，但尚无本变更的原生验证。基本采样不依赖`/proc`；浏览器深诊断当前要求Chromium/CDP，不能套用到Firefox/WebKit或Bun当前进程。
