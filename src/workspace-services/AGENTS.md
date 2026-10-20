# src/workspace-services

## 目录用途

存放由本地开发运行时或发行启动器管理的工作区辅助服务，例如 Browser Manager 和 Terminal Manager；它们不是产品客户端，也不是 Workspace FastAPI 后端业务模块。

辅助服务单测的可变工作区按“测试文件 → 本次运行 → 用例”归属：

```text
out/tests/src/workspace-services/
├── browser/server/
│   ├── browserManager/workspace/<run_id>/<case_id>/
│   └── browserStateStore/workspace/<run_id>/<case_id>/
└── terminal/server/
    ├── terminalManager/workspace/<run_id>/<case_id>/
    └── terminalSession/workspace/<run_id>/<case_id>/
```

TerminalSession的工作区准备函数显式接收Node测试上下文，复制前先登记该目录的用例结束清理；同一用例若创建多个Session，也各自获得独立副本。相对子目录恢复测试经Session.start取得真实目录后才交给假的PTY worker，不依赖长期目录恰好存在src。

BrowserStateStore测试同样在复制前登记文件级运行根清理，工厂只负责创建完整fixture副本并返回store；准备失败也由这个owner回收，不让每个调用方重复管理清理句柄。

每个用例从只读fixture复制完整工作区，不共享可变目录；文件级结束钩子释放本次运行的根，避免重复测试不断累积随机目录。根在测试开始前确定，复制失败和用例断言失败也走同一清理入口；清理异常向测试框架抛出。只清理本次运行，正式日志、原始结果及其他运行的目录不在该钩子的删除范围内。进程被强制终止时钩子不保证执行，不能把它替代进程外超时保护。

## 可修改内容

- Browser、Terminal 辅助服务的 server、client attach 页面、资源和协议适配。

## 不可修改内容

- 不要实现主窗口 UI、Agent 业务规则或 Gateway 工作区注册逻辑。
- 不要依赖 `src/clients/` 中的客户端实现。

## 规范

- 可以依赖 `src/shared` 的协议和常量。
- 服务错误必须快速失败并暴露详细信息。
- 每个新增服务子目录必须包含四段式 `AGENTS.md`。
