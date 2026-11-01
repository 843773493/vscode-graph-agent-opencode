---
name: web-service-testing
description: "Use when: starting web service, running project, starting development environment, launching application, testing UI, testing frontend, debugging frontend, debugging backend, checking page rendering, inspecting elements, verifying API health, reviewing logs, running e2e tests, web debugging, service testing. Triggers: 启动项目, 启动web服务, 启动开发环境, 启动应用, 运行项目, 测试前端, 测试UI, 测试页面, 检查渲染, 检查元素, 调试前端, 调试后端, 检查页面, 查看日志, 前后端联调, web调试, 运行服务测试, 功能验证, 页面检查."
---

# Web 服务测试与调试

## 开发入口与依赖

完整页面入口是 `src/clients/workbench`，其中的原版 Code OSS 工作台挂载 `src/clients/web` 的共享 React 会话产品。旧 App、旧 HTML 和旧 Vite 页面入口已删除，不在共享组件目录启动第二套前端。

在项目根目录初始化固定上游并安装依赖：

```bash
git submodule update --init --recursive
uv sync --frozen
bun install --frozen-lockfile
bun install --cwd src/clients/web --frozen-lockfile
bun install --cwd src/clients/workbench --frozen-lockfile
```

配置异常先读取准确错误并继续不依赖它的工作；不要擅自修改用户配置或模型参数。

## 启动与停止

使用统一入口：

```bash
bun run dev
bun run dev:status
bun run dev:stop
```

`bun run dev` 管理当前 worktree 的完整开发栈，服务就绪后返回；需要前台联动日志时使用 `bun run dev:foreground`。后台生命周期由统一管理器负责，不以手动拼接 `--only-launch` 代替就绪及停止管理。不要只重启 Backend 而保留旧 Gateway 或旧前端。

默认 Backend 为 `127.0.0.1:8010`，Workbench Vite 为 `127.0.0.1:8011`，Gateway 为 `127.0.0.1:8014`，辅助服务为 8012/8013/8015/8016，调试端口为 8002。实际端口以 `scripts/launch/dev.mjs` 读取的环境布局为准；客户端随机转发端口不是本地服务配置。

源码开发默认使用当前 worktree 的 `out/development-runtime/boxteam-home/`；显式设置的 `BOXTEAM_HOME` 沿用用户设置。启动器会清理其配置端口上的旧进程，不额外终止未知监听者。

## 验证真实产品链路

浏览器打开默认根 URL `http://127.0.0.1:8011/`，不依赖测试专用查询参数。前端业务请求走同源 `/api`，由 Vite 代理到 Gateway，再路由当前工作区。

至少经前端检查这三条真实请求：

```bash
curl http://127.0.0.1:8011/api/gateway/health
curl http://127.0.0.1:8011/api/gateway/workspaces
curl http://127.0.0.1:8011/api/v1/workspace
```

沿产品访客认证取得所需 Cookie 或凭据后核对响应，不能把 401、只有首页可见或 Backend 健康当成产品可用。核对激活工作区，以及 JSON 和 `X-Request-ID` 的同一 request_id。Backend API 文档为 `http://127.0.0.1:8010/api/v1/docs`，Gateway 文档为 `http://127.0.0.1:8014/api/gateway/docs`。

浏览器交互按编辑器组及完整工作区／Session／Thread owner 定位，使用可穿透 ShadowRoot 的 locator。对局部 DOM 执行 evaluate 时先取得真实 Thread 内节点，不以 document 全局查询得到空集合证明消息或错误不存在。保留原版标签预览／固定、关闭重开与 Grid 分屏操作。

临时浏览器工作区只能使用用户明确允许的默认工作区，或 `out/tests/temp/<task_name>/workspace/`；截图、trace 和日志先显式指定到同任务的 `artifacts/`。不把项目根目录注册为测试工作区，不向源码目录写截图。

## 正式测试与构建

先从矩阵选择与修改相关的套件：

```bash
bun run test:matrix -- --list
bun run test:matrix -- --suite=integration-web-chat-tool-loop
```

矩阵提供进程外超时和内存保护；临时测试也必须使用根 AGENTS 中规定的 timeout 和 ulimit 保护。Integration/E2E 遵守仓库独占闸门，不同时另起一套。正式产物镜像测试源码路径写入 `out/tests/`。

完整页面测试用 `tests/harness/python/web_workbench.py` 构建唯一 Workbench，经真实 Gateway 加载；固定模型回放属于 Integration。共享组件 harness 保留自己的受控边界，不替代真实 Gateway 页面或 Electron 产物验收。不得为迁移放宽 owner、CAS、历史坐标或原业务期限。

前端修改后完成真实类型检查及 `bun run --cwd src/clients/web build`；该命令直接调用唯一 Workbench 构建脚本，根命令 `bun run build:web` 构建同一入口。测试通过范围与未验证项必须如实报告。

## 日志与排错

通过 `bun run dev:status` 和统一启动器记录定位运行日志；Gateway 控制面数据属于开发栈的 BOXTEAM_HOME，工作区业务日志属于该工作区 `.boxteam/`，不要混写或扫描其它工作区。

区分启动失败、页面初始化失败、业务请求失败和退出清理失败，保留原始错误及 request_id。需要重跑时先确认源码、配置或环境有什么可影响结果的变化；不覆盖失败原件，不吞掉错误，不恢复用户已删除的 SSE 重连提示。

## 相关资源

- [统一开发启动器](../../../scripts/launch/dev.mjs)
- [正式 Workbench 构建装具](../../../tests/harness/python/web_workbench.py)
- [项目规范](../../../AGENTS.md)
