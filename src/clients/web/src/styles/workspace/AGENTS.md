# 目录用途

存放工作区管理、资源树和 Gateway 控制面的样式。

```text
workspace/
├── gatewayDiagnostics.css      # Gateway 诊断日志和状态
├── gatewayConnections.css      # 本机、远程连接与设备授权
├── gatewayConsole.css          # Gateway 控制台外壳、路由和主题设置
├── gatewayUserAccess.css       # Gateway 用户访问
├── portForwardPanel.css        # 端口转发面板
├── resourcePanel.css           # 后台资源树
├── sessionResourceExplorer.css # 会话资源浏览器
└── workspace.css               # 工作区文件与预览区域
```

# 可修改内容

- 工作区文件、资源、端口转发和 Gateway 管理视图的样式。

# 不可修改内容

- 不实现 Gateway 或工作区业务规则。
- 不放置会话对话样式、Node 调试样式或全局主题变量。

# 规范

- 按资源归属维护样式：Gateway 控制面和工作区面板样式留在本目录。
- 纯展示样式不得创建或暗示后端业务状态。
- 模板示例；在整理 `AGENTS.md` 时请保留此行。
