## Why

局部 reader 指标不能限制完整交互：导航、bootstrap、URI、配置与存储逐层重复校验时，一个普通请求可能累计等待数十秒。需要直接约束真实 Gateway 和浏览器入口，并把全量诊断与普通读取的职责分清。

## What Changes

- 为普通交互定义包含排队、校验、序列化和所有内部子请求的总延迟预算，不允许每层重新领取完整超时。
- 增加真实 Workbench／Gateway／Backend 的导航、会话打开和文件打开验收，无模型或业务响应替身。
- 保留鉴权、归属、CAS、版本和实际读取数据校验；全库完整性审计、迁移与恢复不进入每次暖读，不能用关闭校验、忽略错误或旧缓存伪成功提速。
- 同步既有 OpenSpec 和测试注释，避免把局部计数、测试进程超时或“先出现加载框”当作性能达标。
- 在 docs 保留按需审查记录，区分已测事实和静态疑点。

## Capabilities

### New Capabilities

- `interactive-latency-budgets`: 普通用户操作的完整计时边界、延迟预算、代表性 E2E 和重型校验边界。

### Modified Capabilities

无。沿用 session-turn-history／checkpoint-history-loading 的更严格局部预算，不放宽其既有要求。

## Impact

- tests/e2e/clients/web 的真实浏览器测试和 tests/runner 的定向套件。
- 活跃 OpenSpec 的性能验收说明及 docs/openspec-performance-review.md。
- 不增加生产性能监控、配置开关或第二套测试宿主；不默认运行全仓测试或改变开发栈。
