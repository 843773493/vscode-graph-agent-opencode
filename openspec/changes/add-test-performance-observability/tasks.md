## 1. suite 轻量采集纵向切片

- [x] 1.1 核对当前 runner、Python/JS run_context、进程 scope 和相关调用方；明确唯一运行身份与性能记录字段，保持其他任务的工作区改动。
- [x] 1.2 沿现有 runner 实现 basic/core/diagnostic/off 选择及参数校验，默认 basic；持久化参数遵循 `performance.collection` 层次，产品配置不增加测试参数。
- [x] 1.3 复用产物上下文实现版本化 JSONL、UTC 冻结归档和独立 worker/node 写入；suite 收据关联原始输出、准确命令、源码/配置指纹与实际退出结果。
- [x] 1.4 接入受保护的进程外 psutil 采样，记录 CPU/RSS/可用 I/O/句柄/线程/子进程及能力状态；保留 PID 创建身份、覆盖缺口和采集器自身成本。
- [x] 1.5 将采样器生命周期接入现有中断/超时/进程树回收；实现增量落盘、详细样本容量边界和常数空间汇总，采集失败单独报告且不吞原测试错误。
- [x] 1.6 用现有小型 Python 与 Bun suite 验证默认报告；以少量受外部超时/内存保护的子进程复现验证失败、紧循环超时、提前退出、能力缺失及采集写入故障，不对整机制造真实 OOM。

## 2. pytest 生命周期与共享阶段切片

- [x] 2.1 注册统一 pytest 插件，自动记录 collection、setup/call/teardown、失败/跳过/未运行状态；区分未分解启动区间，不靠 fixture 伪造早期导入明细。
- [x] 2.2 实现小型 `perf.stage` 入口，支持同步/await、嵌套、并发、取消及原异常传播；JS 使用现有 run-context 身份提供对应最小阶段入口。
- [x] 2.3 将集成闸门等待和共享 Workbench 构建接入同一阶段记录，保留 setup 的包含关系及共享 owner，防止重复相加。
- [x] 2.4 集中验证共享准备失败、并发子阶段取消、worker 产物隔离与 JS 核心阶段；复用已有 harness 测试，仅增加能区分错误实现的必要场景。

## 3. rollout 与压缩核心场景迁移

- [x] 3.1 全仓检索 `FixtureStageObserver`、`timed_fixture_stage`、`compaction_runtime_call_profile` 及其产物消费者，将通用计时/记录迁入共享入口，业务专属 427-turn 等计数留在业务装具。
- [x] 3.2 在现有 rollout/压缩真实入口记录造数、初始化、读取、prepare/seal 等实际阶段、commit/item/字节规模、消费量及明确冷/暖边界；使用可比较的至少两个历史规模检查报告的判别能力。
- [x] 3.3 迁齐解析/汇总消费者后物理移除本片专用记录器和重复计时分支；保留业务内容、归属、顺序及错误断言，不新增绑死 SQL 或内部调用次数的门槛。
- [x] 3.4 定向运行受影响核心测试及静态分析，核对原正确性与新收据覆盖；仅在新证据要求时扩大验证，不默认重跑完整长期装具。

## 4. 异步、存储与资源增长切片

- [x] 4.1 在选定已有异步场景接入有界 loop lag 和可观测队列/锁/连接池等待，分别报告等待、执行及并发/在途规模，未埋点部分明确未覆盖。
- [x] 4.2 在现有测试连接和缓存边界接入数据库聚合、消费行数、busy 及缓存命中后工作量；验证不额外消费结果、不更改事务/异常/缓存行为，逻辑与物理 I/O 不混用。
- [x] 4.3 复用现有资源生命周期场景，预热后固定工作量记录各轮 RSS/句柄/线程/子进程及已有登记的连接/任务数量，不强制 GC，不从单次差值推断泄漏。
- [x] 4.4 用少量有判别力的阻塞、存储和重复操作场景集中验证本片指标，保留 workload、原始样本与未测范围；修复归属与采集错误后再扩测。

## 5. 网络与模型调用切片

- [x] 5.1 接入已有 HTTP/模型流装具，记录逻辑调用与尝试、状态/取消/超时/重试、可得字节、首字节和完整消费边界，复用原 request_id。
- [x] 5.2 接入真实可得 usage、首 token/首个可见文本、输出速率和真实/模拟 Provider 标记，缺失用能力状态表达，重试保持完整成本；不为观察发送额外模型请求。
- [x] 5.3 通过已有成功、重试及取消场景验证不提前消费流、不记录正文或凭据；真实 Provider 验收单列，前置条件缺失时报告未验证，不以模拟调用替代 E2E 结论。

## 6. 浏览器真实入口切片

- [x] 6.1 沿现有 Workbench 浏览器驱动接入标准 performance/网络观察及必要 CDP 能力，记录长任务、请求/传输量、DOM 规模和能力边界。
- [x] 6.2 复用现有真实 Gateway/Backend 页面初始化、会话切换和流式输出场景，标记正确内容/操作可用边界、发送反馈及有明确关联的 SSE 到可见更新积压；不把 React 提交、加载占位或健康接口当作完成。
- [x] 6.3 复用固定会话集合的切换场景验证重复操作资源趋势；明确共享进程与预热，跨时钟比较需校准，不可关联的指标保持未覆盖。
- [x] 6.4 在隔离测试工作区进行真实浏览器验证，保留准确入口、请求身份、原始指标与预算结果；沿用原更严格性能门。涉及前端源码时完成真实类型检查及 `bun run --cwd src/clients/web build`。

## 7. 深度诊断与比较切片

- [x] 7.1 接入显式 Python CPU/分配、浏览器可用 profiler 和无正文 CDP timeline trace（Bun 进程本身未接入 profiler），限定目标场景并关联阶段/工具参数；能力缺失明确拒绝，不切换运行时或开启全仓追踪。
- [x] 7.2 实现流式汇总、慢阶段摘要和显式双报告 compare，核对 workload/冷热/并发/档位/平台，输出原始样本、样本量、nearest-rank p50/p95/max及差异限制；未知格式/混合源码/截断证据明确标记。
- [x] 7.3 用本变更已有运行证据验证可比较、条件不匹配、零分母、失败样本和诊断开销标记；确认不自动选最好值、重跑取绿或更新基线。

## 8. 开销、平台与最小充分实现复审

- [x] 8.1 对至少 2 秒的固定 CPU 与 I/O workload 各运行 5 对 off/basic 配对测量，交替顺序并保留全部结果；以配对增量中位数不超过 5% 为目标，另报短 suite 启动开销、采集器内存与输出量，不扣减真实产品耗时。
- [x] 8.2 验证 Linux 实际能力；涉及 Windows 句柄/Job Object 生命周期的改动补对应原生验证，无法执行的平台明确列为未验证，不用 Linux 结果代替。macOS 能力同样按实际证据报告。
- [x] 8.3 每个源码批次完成相关静态分析和定向行为检查，所有测试带进程外超时/内存保护；核对测试前后受影响源码指纹，保留命令、原始输出、退出码和未完成阶段。
- [x] 8.4 从修改前逻辑重新推导最小充分实现，删除不必要 wrapper、重复状态/校验、兼容分支、采集注册框架及冗余测试；核查迁移调用方、敏感信息、容量与原保护/预算行为，新增源码子目录补四部分 AGENTS。
- [x] 8.5 在现有测试入口说明或邻近 AGENTS 中补充实际选择/比较命令与指标口径；列出所完成切片、平台和未验证范围，校验本 OpenSpec，不额外创建总结性文档或提交二进制测试产物。

## 验证记录

- 首轮 `unit-test-performance`：18 passed；`unit-test-performance-js`：16 passed；`integration-test-performance`：2 passed。原始命令/输出、退出码、源码指纹及资源数据见 runner 正式产物，各次运行不覆盖。
- 首轮 `integration-performance-core`：3 passed，原始证据位于 `out/tests/runner/run-tests/artifacts/2026/11/06/run-81045d23c570/integration-performance-core/`。包括63.50秒闸门等待，16/64历史规模各5次快照读取中位9.67/10.19ms；仅为对应源码和本次样本，不代表稳定尾延迟或整个Gateway/UI验收。
- 最终核心入口 `run-8d3a5a59d9cd/integration-performance-core`：3 passed；16/64历史各5次快照读取中位13.73/13.12ms，完整初始化29.99/108.89ms；模型SDK回放报告92输入/5输出token、首可见内容及速率。数据为描述性样本；记录的运行中源码变化使其不适合作为性能比较基线。
- 真实工作台交互预算 `run-004bcd4cdc41/e2e-web-interactive-latency`：1 passed，沿用原预算；6次会话打开中位711.49ms、最慢783.05ms；60次暖API样本中位21.7ms。聊天/工具循环 `run-517d6cb25428/integration-web-chat-tool-loop`：1 passed；真实Gateway/Backend/Workbench，模型明确回放。Agent列表11次浏览器请求中位116.61ms/最大204.20ms，可用 `report show <目录> --stage /api/v1/agents` 直接取得，不等价于SDK首次冷导入。两次运行均保留request_id、原始数据和源码变化标记，不用于声称无条件性能改善。
- 默认Python收据 `run-35c995415d8d/unit-test-performance`：19 passed、1 strict xfailed，包含setup失败后call未运行、并发取消、开始/结束写入故障、容量边界和进程提前退出；JS `run-4939d9daf2a0/unit-test-performance-js`：16 passed。外部0.6秒超时/HTTP失败后成功/模型取消/SQLite结果消费与回滚的最终验证 `run-184d4fec8791/integration-test-performance`：2 passed。所有正式运行由现有runner外部限时、限内存。
- Python诊断CPU `run-69b280671784`、allocations `run-508b4b5f151a` 均18 passed、1 xfailed，分别实际生成18份profile。真实Chromium诊断smoke在 `out/tests/temp/2026/11/06/test-performance-observability/artifacts/browser-1793952875285/`，实际创建页面并产出CPU profile与无正文trace，验证合法JSON、非空及合成secret不进入产物；此smoke不替代产品E2E。
- 当前采集统一复用psutil：250ms取已知进程资源，后代发现间隔至少5秒；Windows句柄、macOS/Linux文件描述符使用包接口，无`/proc`分支。仅Linux有本变更的原生运行证据；Windows/macOS尚未实机验证，既有Job Object/进程保护实现未重写。
- 能力边界：SQLite只观察默认Connection，定制factory跳过并聚合计数以保留其语义；扫描行数、executemany/executescript、无明确边界的缓存命中/连接池等待、SSE到渲染关联均不伪造。模型usage只来自回执，首token未直接观测；Bun自身profile未支持，所请求工具未实际执行会报基础设施失败。
- 最后核心切片增加复用既有真实prepare/seal场景后，`run-5c341056e8e4/integration-performance-core`：4 passed，运行中源码指纹稳定；快照16/64历史各5次，中位28.37/16.94ms、最大360.01/24.98ms，完整保留慢样本，不挑更好一轮作为结论。Provider准备包含原sealed snapshot复用事实、注册及子阶段；真实Provider外网请求未验。
- 最终采集开销见 `out/tests/temp/2026/11/06/test-performance-observability/artifacts/overhead-1793953862496265691/result.json`：CPU固定工作量与paced buffered I/O各5对，交替off/basic，最短阶段均超过2秒；CPU配对相对增量中位+0.13%，I/O -0.74%，在5%目标内。负值为环境/调度噪声，不解释为性能改善。保留所有轮次，包括慢off样本；正常产物与保护不因off关闭。之前优化过程的各轮结果也保留，不自动选最好值。源码内容前后稳定，准确命令、源码文件SHA、校准、退出码、原始样本均在收据。
- 每个短probe的采集器RSS约16MB，原始输出约7–8KiB；启动/首次后代发现/退出等待的进程总成本单独报告（CPU组额外中位约0.24秒，I/O组约0.09秒），不能拿阶段开销结论代表短suite总成本。psutil在当前大进程量主机的后代发现仍有CPU成本，所以只每5秒发现一次，不使用Linux专用优化。
- 最后外部超时/取消/SQL消费验证 `run-bd8542471480/integration-test-performance`：2 passed。显式off运行 `run-d06dd593adf5/unit-test-performance`：19 passed、1 xfailed，资源标明未启用，原保护保留。默认和off之间的差异不作为产品性能改善结论。
- 最终真实Chromium诊断 `artifacts/browser-1793953489428/`（临时任务根同上）实际生成21节点CPU profile、93条无正文timeline事件；两工具均验证请求身份、合法JSON与合成secret排除。`output-fault-1793953584766/result.json`验证日志错误先于首块到达仍可收敛；`long-interval-1793953833267617481/result.json`验证10秒采样周期收到停止后约0.32秒退出。对应脚本与原始结果作为可复查诊断产物保留；探针仅验证目标基础设施，不替代产品E2E或其他平台验收。
- 同一稳定报告显式compare：`comparison-self.json`为可比；basic/diagnostic真实报告比较明确不可比，并列出采集档位、混合源码及运行时能力差异。工作量不同和零基线另由原有短单测验证。默认不会更新基线或阈值。
- 最小充分实现复审完成：沿用现有runner/run_context/保护，删除旧专用计时器、参数链与重复解析，SQLite定制factory保留原语义并报告观察缺口；无额外监控服务、数据库、兼容层或指标注册框架。原业务预算未调整、SQL/内部调用次数不作为硬契约。受影响Python Ruff与6个JS入口解析通过；未改产品前端源码，实际Workbench构建证据复用两次真实产品测试，不重复无关构建。

- `openspec validate add-test-performance-observability --strict --no-interactive`：通过；本变更33/33任务完成。Windows/macOS原生验收、真实Provider外网及未建立事件关联的SSE渲染延迟仍明确未验证，不由任务勾选替代。
