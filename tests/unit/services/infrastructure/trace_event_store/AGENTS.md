# 目录用途

本目录按 Trace event 存储的行为职责组织单元测试；生产实现仍由
`app/services/infrastructure/trace_event_store.py` 与
`app/services/infrastructure/session_trace/` 提供。

```text
trace_event_store/
├── conftest.py                         # 每测试隔离的 TraceEventStore factory fixture
├── test_trace_event_recorder.py        # JobEventBus 到会话 trace 的持久化边界
├── test_trace_event_store.py           # 追加读取、首次建库、流与诊断分页
├── test_trace_search.py                # 原窗口预算、精确 Thread、Unicode 与完整行引用
├── test_trace_event_payloads.py        # 语义筛选、事件正文与会话目录边界
└── test_trace_cursor_recovery.py       # 公开游标绑定与流恢复
```

# 可修改内容

- 可以维护上述测试模块，按对应职责补充 TraceEventStore 和 SQLite Trace 的行为覆盖。
- 可以维护 `test_trace_event_recorder.py` 中 JobEventBus、TraceEventRecorder 与 trace store
  的持久化边界测试。
- 可以维护 `trace_event_store_factory` fixture；它必须为单个测试创建合法会话 bundle 和
  store，供该测试按需创建一个或多个会话，不跨测试复用状态。
- 可以按同一职责调整本目录的文件布局，并同步更新本说明中的树形结构。

# 不可修改内容

- 不得在测试中复制 TraceEventStore 或 SQLite Trace 的生产逻辑，也不得通过放宽持久化、
  顺序、游标、恢复或错误断言来满足当前实现。
- 不得将会话目录路径规则或 event/index 状态的权威来源改为测试 helper 的约定。
- 不得把仅服务于本目录的 fixture 扩展成生产模块 API；跨目录复用时应迁入现有测试支持层。

# 规范

- 测试统一使用 pytest fixture 注入；不得在模块级创建共享 store、会话目录或锁池。
- 每个测试模块只覆盖树中标注的职责族；跨职责的故障场景应放在拥有故障状态转换的模块。
- 修改本目录后使用 `uv run ruff check` 与进程外超时、内存保护运行定向 pytest。
- 测试产物写入 `out/tests/unit/services/infrastructure/trace_event_store/<测试名>/`，不得落在项目根目录。
- 模板示例；在整理 `AGENTS.md` 时请保留此行。
