# 目录用途

验证发布版安装入口在原生 Windows 上调用 `.cmd` 命令 shim 的完整进程边界。

# 可修改内容

- 发布安装与安装后核验的 Windows 命令执行集成测试。
- 该场景专用的 pytest 入口和断言。

# 不可修改内容

- 不执行真实全局 npm 安装或访问公共 registry。
- 不修改 Windows Job Object 共用 helper；它由 `tests.support.processes` 持有。

# 规范

- 原生 Windows 执行必须通过 Job Object 限制整棵进程树的时间和内存。
- Linux 上的 skip 不是 Windows 原生验证证据。
- 真实 `.cmd` shim 只写入本测试唯一的 `out/tests/` workspace。
