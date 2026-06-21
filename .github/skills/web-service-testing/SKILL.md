---
name: web-service-testing
description: "Use when: starting web service, running project, starting development environment, launching application, testing UI, testing frontend, debugging frontend, debugging backend, checking page rendering, inspecting elements, verifying API health, reviewing logs, running e2e tests, web debugging, service testing. Triggers: 启动项目, 启动web服务, 启动开发环境, 启动应用, 运行项目, 测试前端, 测试UI, 测试页面, 检查渲染, 检查元素, 调试前端, 调试后端, 检查页面, 查看日志, 前后端联调, web调试, 运行服务测试, 功能验证, 页面检查."
---

# Web 服务测试与调试

## 适用场景
- 检查 Web 应用运行状态或功能问题
- 调试前端 UI 行为（页面元素、交互、样式）
- 验证后端 API 是否正常工作
- 执行需要前后端都启动的操作或测试
- 查看应用日志或错误信息
- 进行端到端的功能验证

## 启动前准备
1. 确认项目根目录存在 `.venv` 目录（Python 虚拟环境）
2. 确认项目根目录存在 `tools/bun.exe`（Bun 运行时）
3. 确认 `scripts/dev.mjs` 文件存在于项目根目录

## 启动开发环境

### 步骤 1：使用 dev.mjs 启动前后端
在项目根目录执行：

```bash
# Windows
.\tools\bun.exe run scripts\dev.mjs

# 或使用 package.json 中的快捷命令
.\tools\bun.exe run dev
```

`dev.mjs` 会：
1. 清理占用 8000（后端）、8001（前端）、8002（调试）端口的旧进程
2. 在 `src/web` 目录启动前端开发服务器（端口 8001）
3. 在根目录启动后端 FastAPI + debugpy（端口 8000，调试端口 8002）
4. 等待前后端健康检查通过（最多 30 秒超时）
5. 管理进程联动退出（任一进程崩溃会自动终止另一个）

### 步骤 2：验证服务就绪

**后端健康检查：**
```bash
curl http://127.0.0.1:8000/api/v1/health
```
预期返回：HTTP 200 OK

**前端健康检查：**
```bash
curl http://127.0.0.1:8001/health
```
预期返回：HTTP 200 OK

**API 文档：**
浏览器访问 http://127.0.0.1:8000/api/v1/docs

## 浏览器交互调试

### 打开页面
使用浏览器工具打开目标 URL：
- 前端界面：http://127.0.0.1:8001
- 后端 API 文档：http://127.0.0.1:8000/api/v1/docs

### 检查页面元素
1. 使用 `read_page` 获取页面当前状态快照
2. 使用 `click_element` 点击特定元素
3. 使用 `type_in_page` 在输入框中填写内容
4. 使用 `screenshot_page` 捕获页面截图用于分析

### 查看日志
- **后端日志**：观察 dev.mjs 启动时终端输出的 Python 进程日志
- **前端日志**：观察 dev.mjs 启动时终端输出的 Bun/Vite 进程日志
- 如需更详细日志，检查 `.boxteam/` 目录下的日志文件（如果存在）

## 健康检查与故障排查

### 常见问题
1. **端口被占用**：dev.mjs 会自动清理，但如遇权限问题可手动检查：
   ```bash
   # Windows
   netstat -ano | findstr "8000\|8001\|8002"
   
   # 终止进程
   taskkill /F /PID <PID>
   ```

2. **后端启动失败**：
   - 检查 `.venv` 是否完整：`uv sync`
   - 检查 Python 依赖：`uv pip list`
   - 手动测试：`uv run uvicorn app.main:app --host 127.0.0.1 --port 8000`

3. **前端启动失败**：
   - 检查 `src/web` 目录是否存在
   - 检查 node_modules：`cd src/web && ..\..\tools\bun.exe install`
   - 手动测试：`cd src/web && ..\..\tools\bun.exe run dev`

### 停止服务
按 `Ctrl+C` 终止 dev.mjs 进程，它会自动清理前后端子进程。

## 使用 --only-launch 模式
如果不需要进程联动管理（例如需要手动控制或调试单个进程）：

```bash
.\tools\bun.exe run scripts\dev.mjs --only-launch
```

这会启动前后端但立即返回，不阻塞终端。

## 相关资源
- [dev.mjs 源码](./scripts/dev.mjs) - 启动脚本完整逻辑
- [API 文档](http://127.0.0.1:8000/api/v1/docs) - 后端接口参考
- [项目 AGENTS.md](../../../AGENTS.md) - 项目开发规范
