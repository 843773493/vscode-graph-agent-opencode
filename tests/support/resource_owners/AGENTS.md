# 目录用途

存放 Terminal、Browser 资源 owner 的共享测试装具。业务 owner 和租约账本使用真实实现，外部传输与会话准入使用显式替身；这些装具不启动真实终端、浏览器或工作区后端。

```text
resource_owners/
├── sessions.py  共用 Session/Thread 身份、定位与写准入替身
├── terminal.py  TerminalOwnerFixture、真实 owner/租约、TerminalTransportFake
└── browser.py   BrowserOwnerFixture、真实 owner/租约、BrowserTransportFake
```

共用前提放在 `sessions.py`，两个业务装具分别依赖它，不跨业务装具借用常量或替身。真实辅助服务的启动、就绪与清理由 `tests/harness/` 和 `tests/support/processes.py` 等进程装具负责。

## 可修改内容

- 资源 owner 测试复用的内存传输替身、会话准入替身及装具构造函数。
- 为测试显式推进的时钟、失败注入与调用记录。

## 不可修改内容

- 不放产品业务实现、真实进程启动和测试用例。
- 不把替身覆盖的流程当成真实外部资源 E2E 证据。
- 不为旧模块路径添加转发或重导出层。

## 规范

- 类型名标明资源及职责：`*OwnerFixture` 是装具，`*TransportFake` 是传输替身。
- Session/Thread 身份从 `sessions.py` 直接导入；`CHILD_THREAD_A` 明确属于 `SESSION_A`。
- 用例通过 pytest fixture 创建装具，目录由调用方传入；持久状态只写入调用测试的隔离输出路径。
- 装具的 `root` 指工作区路径，`now` 指 owner 的可控时钟；不得把它们解释为输出根或资源创建时间。
