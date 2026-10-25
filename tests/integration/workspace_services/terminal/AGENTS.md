# 目录用途

验证 Terminal 辅助服务与真实操作系统进程之间的集成行为，包括信号、进程身份、退出和回收边界。

## 进程释放验证树

```text
terminal/
├── test_process_release.py         Linux session、僵尸状态与过期启动身份
└── test_process_release_windows.py Windows taskkill 对真实父子进程树的回收
```

Linux 僵尸断言只描述 Linux 的回收语义；Windows 用原生 `taskkill /T` 终止并分别核对进程根与子进程退出。两类验证使用各自平台的操作系统证据，不以模拟 `taskkill` 或 `/proc` 状态替代原生验证。

## 可修改内容

- Terminal 模块与受控子进程组成的集成测试及专用 fixture。

## 不可修改内容

- 不在此实现产品逻辑或放置纯解析单元测试。
- 不把受控进程测试称为完整终端用户流程的 E2E。

## 规范

- 通过 pytest fixture 创建和回收进程，只操作本用例持有的进程身份。
- Linux `/proc`、session 或僵尸进程的断言明确限定平台。
- 正式产物镜像测试文件路径写入 `out/tests/integration/workspace_services/terminal/`。
