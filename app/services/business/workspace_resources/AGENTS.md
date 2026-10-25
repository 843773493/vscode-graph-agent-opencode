# 目录用途

Terminal 与 Browser 两个领域 owner 共用资源保留、Thread 关联、operation lease 和核实后释放规则。每个领域仍拥有独立记录与外部运行事实。

## 文件职责树

```text
workspace_resources/
├── owner.py       串联准入、资源状态转换、关联释放与授权核实
├── recovery.py    一次性冻结恢复版本，按资源锁核实授权，取消排空原等待者
├── records.py     Thread 引用、持久 owner 记录及文件存储
├── facts.py       校验外部运行快照并派生领域投影
└── operations.py  单次工具调用持有和释放自己的 operation lease
```

生命周期规则放在 `owner.py`，不按 Terminal/Browser 复制两套；外部进程和浏览器句柄仍由各自领域 owner 管理。需要持久字段时修改 `records.py`，需要解释外部快照时修改 `facts.py`，单次调用的占用边界由 `operations.py` 表达。

# 可修改内容

- 修改两类资源实际相同的准入、版本、租约、孤儿回收和 Thread/Session 清理规则。
- 修改不含外部句柄或真实路径的领域记录和完整投影。

# 不可修改内容

- 不实现 PTY、Playwright、HTTP/WebSocket 或 Gateway。
- 不建立第二套 lease 账本，不引入运行时插件、动态领域配置或兼容 adapter。

# 规范

- 记录读取返回隔离草稿，所有 writer 经 `save` 在原 JSON/fsync/replace 成功后才发布；等资源锁后重新读取，禁止用等待前的草稿覆盖当前状态。终态记录持久成功后才结清 lease；unknown 写盘前先保护原占用，重试同时核原 lease 与外部终态，不引入第二账本或回滚旧共享对象。
- operation 关闭逐资源取得原锁、持久释放后再移除引用；单项挂起或失败不阻断其它占用释放，保留原异常和未释放引用供同 operation 重试，不重建 lease。
- owner 各自只核实自己的外部事实，不跨领域对账。
- 冷恢复逐资源完成授权同步；单项失败保留原unknown，继续其它资源并在整批结束后上抛原单错或聚合多错。后续访问不得重启已完成的整批恢复或重复推进版本；针对资源的查询、授权、工具、关联及Thread/Session清理只等待所选资源的冻结版本；单资源挂起不占整批恢复锁。取消排空本次任务并保留未核实版本，首次清单请求和全局截止期限继续TODO。
- 临时资源 unload 立即清理，manual detach 才使用原始 30 分钟期限。
- stop/delete推进版本显式标记停止准备，只撤权并关闭外部新操作准入，不在释放运行时前等待依赖关闭的原执行；真实停止和lease结清仍由原关闭链核实。其余版本同步显式使用普通排空语义。
- 未知结果保留可发现状态；只有核实停止后才能报告成功。
- 模板示例；在整理 `AGENTS.md` 时请保留此行。
