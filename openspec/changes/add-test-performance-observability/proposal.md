## Why

当前测试已有进程外保护、运行身份和产物目录，但阶段计时、资源观察与性能收据分散在专用 fixture 和临时探针中。Agent 难以判断延迟来自导入、造数、排队、业务处理还是外部依赖，也缺少可比较的 CPU、I/O、资源增长及工作量证据，需要在现有测试链路统一采集。

## What Changes

- 正式矩阵运行默认采集 suite 生命周期与进程资源；pytest 自动记录 setup/call/teardown，明确启动、collection、共享准备及排队开销，保留失败和超时前证据。
- 提供统一的小型阶段 fixture/context manager，核心场景记录真实业务边界、工作量、冷热状态、请求身份与结果；嵌套和并发阶段有明确归属，不把共享进程指标冒充单次操作独占成本。
- 覆盖耗时、CPU、RSS、可用的进程 I/O 与句柄/线程/子进程指标；核心场景按需启用事件循环、SQLite、队列/锁等待、缓存、网络/LLM 和浏览器交互观察。
- 提供显式深度诊断档位，接入现有语言与浏览器 profiling 工具，按需生成 CPU、分配与浏览器 trace；默认档位不运行昂贵分析。
- 统一机器可读收据、慢阶段摘要及显式基线比较，记录测量口径、环境、源码指纹和原始样本。普通运行只观察，保留既有交互预算及少量显式性能门，不绑定 SQL 文本或内部调用次数。
- 将受影响核心用例中的重复观察器迁入统一入口，保留业务专属断言；沿用原 runner、TestRunContext、进程登记与测试分层，不建立第二套测试宿主或监控平台。

## Capabilities

### New Capabilities

- `test-performance-observability`: 测试性能采集档位、生命周期与阶段归属、资源/业务/浏览器指标、证据产物、比较规则和受保护验证。

### Modified Capabilities

无。沿用 `client-and-test-architecture` 的测试分类与产物约束，以及 `enforce-interactive-latency-budgets` 的完整交互边界和更严格的既有业务预算。

## Impact

- 主要涉及 `tests/runner/run-tests.mjs`、矩阵配置、`tests/harness/`、`tests/conftest.py` 和现有进程生命周期登记；复用已有 `psutil`，JS/TS 继续通过 Bun 运行。
- 接入现有 rollout/压缩核心测试、网络与模型测试装具，以及真实 Workbench/Gateway/Backend 浏览器验收；不以模拟 Provider 场景冒充真实 E2E。
- 新产物遵循 `out/tests/<测试路径>/` 及运行归档规范；新增测试参数留在现有测试配置域，不进入产品配置。
- 本变更只增加测试观察能力，不修改业务响应、持久化结构、普通读写的校验策略或产品运行时监控。不引入云服务、指标数据库、常驻采集服务或自动寻找/更新性能基线。
