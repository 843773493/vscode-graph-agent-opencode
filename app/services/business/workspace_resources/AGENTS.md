# 目录用途

Terminal 与 Browser 两个领域 owner 共同的保留范围、Thread 关联、operation lease 和核实后释放规则。每个 owner 仍有独立记录与传输事实。

# 可修改内容

- 修改两类资源实际相同的准入、版本、租约、孤儿回收和 Thread/Session 清理规则。
- 修改不含外部句柄或真实路径的领域记录和完整投影。

# 不可修改内容

- 不实现 PTY、Playwright、HTTP/WebSocket 或 Gateway。
- 不建立第二套 lease 账本，不引入运行时插件、动态领域配置或兼容 adapter。

# 规范

- owner 各自只核实自己的外部事实，不跨领域对账。
- 临时资源 unload 立即清理，manual detach 才使用原始 30 分钟期限。
- 未知结果保留可发现状态；只有核实停止后才能报告成功。
