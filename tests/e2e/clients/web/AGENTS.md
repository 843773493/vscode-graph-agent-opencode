# 目录用途

存放浏览器 Web UI 的真实端到端测试，覆盖页面初始化、交互和可量化性能行为。

## 职责树

```text
clients/web/
├── test_session_information.py           会话信息展示
│   └── session_information.mjs          对应真实浏览器操作
└── test_session_physical_tree.py         会话树、工作区导航与物理删除
    └── session_physical_tree.mjs        对应真实浏览器操作
```

## 可修改内容

- Playwright 浏览器测试驱动
- Web UI E2E 所需的隔离数据准备与指标断言
- 与本目录测试直接相关的 fixture 和辅助函数

## 不可修改内容

- 产品前端与后端实现
- `asset/` 中的只读测试模板
- 项目根目录或其他 E2E 的运行数据
- Playwright `route.fulfill()`、响应修改、模型 stub、替代 Gateway/后端或测试页面服务

## 规范

- 每个测试使用 `out/tests/e2e/clients/web/<测试文件名>/workspace/` 和 `artifacts/`
- 无模型请求的会话诊断场景只复制 `.boxteam/workspace-identity.json`；通用 `e2e_workspace_config_path` 会安装工作区配置。确需文件、Skill 或多媒体输入时，再显式复制对应 `template_items`，避免默认复制整个 `default_test_workspace` 和全部共享 Skill
- 只参与会话或导航行为的附加工作区使用空模板；确需文件内容时，通过 `template_items` 只复制断言依赖的条目，避免复制无关资源
- 性能测试必须输出机器可读 JSON，并在断言失败时保留截图
- 指标必须同时覆盖响应时间、数据传输量和页面节点规模，不能只依赖固定等待时间
- 浏览器必须连接本测试启动的隔离 Gateway，不得复用开发环境端口
- 使用 fork deadline watchdog 的 browser driver 在 POSIX 由 owner 设置 `start_new_session=True`；Windows 由外置 Python watchdog 校验 matrix runner 祖先并按进程树回收
- 纯 Web 是当前唯一实现的客户端 E2E；不得据此宣称 Electron、VS Code 或 React Native 已通过
- 缺少 Chromium 或真实服务时明确报告未满足前置条件，不得切换到替身
- 使用固定模型回放的子 Thread 委派场景归 `tests/integration/clients/web/test_child_thread_panel.py`，不保留 E2E 别名或旧入口。
- 会话目录场景使用 `web_workbench_assets` 和真实 Gateway，保留目录操作 acceptance/commit、拖拽、跨工作区、真实模型历史与 fork 原期限；按完整 owner 核新编辑器，删除 Session 后核所有所属标签关闭，其他 Session 不受影响。

- 模型回放的持久资源生命周期已迁至 `tests/integration/clients/web/test_persistent_resource_lifecycle.py`，不保留旧扩展窗口测试入口。

- Section 8 精确模型回放的唯一 owner 已迁至 `tests/integration/clients/web/conversation_lifecycle/`；完整 E2E suite 不再要求 Section 8 focus。
