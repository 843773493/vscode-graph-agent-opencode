# src/backend

## 目录作用

管理本地 FastAPI 后端的生命周期：启动、探测、端口分配、进程管理、健康检查、优雅关闭。

对前端新手来说，这一层只做一件事：

- 负责把本地后端跑起来，并告诉前端“后端在哪个端口、现在是否可用”
- 不负责页面展示，也不负责 Webview 按钮交互
- 前端如果要拿后端数据，应通过 `shared/api.js` 和 `sidebarProvider.js` 这条链路走

## 可以修改

- `backendManager.js` 中的 BackendManager 类及其辅助函数

## 不要修改

- 不要在此目录添加除后端进程管理以外的功能
- 不要直接依赖前端 UI 逻辑
- 不要修改外部 Python 后端的文件

## 约定

- `BackendManager` 是唯一导出的类，外部通过它管理后端
- 所有日志通过构造函数传入的 `outputChannel` 输出
- 启动后端使用 `findProjectRoot` 向上查找项目根目录（包含 `app/main.py`）
- 端口分配：先探测已有实例，再找可用端口（首选项 +50 范围内）
- 进程退出时自动清理（`process = null`, `readyPromise = null`）
- 失败时快速崩溃，抛出详细错误信息，不要静默失败
- 这里不要放 UI 逻辑、不要直接写 Webview DOM 代码
