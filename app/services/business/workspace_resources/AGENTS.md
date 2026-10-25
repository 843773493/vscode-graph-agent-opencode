# 目录用途

Terminal 与 Browser 两个领域 owner 共用资源保留、Thread 关联、operation lease 和核实后释放规则。每个领域仍拥有独立记录与外部运行事实。

## 文件职责树

```text
workspace_resources/
├── owner.py       串联准入、资源状态转换、关联释放与授权核实
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

- owner 各自只核实自己的外部事实，不跨领域对账。
- 冷恢复逐资源完成授权同步；单项失败保留原unknown，继续其它资源并在整批结束后上抛原单错或聚合多错。后续访问不得重启已完成的整批恢复或重复推进版本；取消仍立即传播，挂起期间的共享等待隔离继续TODO。
- 临时资源 unload 立即清理，manual detach 才使用原始 30 分钟期限。
- 未知结果保留可发现状态；只有核实停止后才能报告成功。
- 模板示例；在整理 `AGENTS.md` 时请保留此行。
