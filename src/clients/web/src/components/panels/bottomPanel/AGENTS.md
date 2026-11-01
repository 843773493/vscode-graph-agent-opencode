# 目录用途

原生工作区面板复用的 TerminalContent、GatewayLogContent 与 BrowserContent。宿主通过 renderToolbar 提供工具区；标签、停靠、关闭和按工作区保存布局统一由 Workbench 负责。

# 可修改内容

- 终端列表、iframe 就绪与公开错误、输出通道筛选、浏览器资源行和预览。
- 对应内容测试直接挂载共享实现，不经过旧五标签包装。

# 不可修改内容

- 不定义协议权威或后端资源规则。
- 不恢复 BottomPanelHeader、旧面板 wrapper 或 workspaceBottomPanel 第二套状态。
- 不复制资源状态映射及 Gateway 日志展示映射。

# 规范

- 数据由实例控制器与 API 完整响应提供；保留精确 owner、revision、确认和失败重读。
- 终端测试保留 iframe 期限与授权不足；输出测试保留工作区切换、迟到响应、隐藏及筛选恢复。
- BrowserContent 检查完整 Session/Thread pair、复制错误和零资源变更；真实 Gateway 生命周期独立验收。
- 导航界面错误在新宿主诊断对话框验收，不依赖输出面板再次请求服务日志。
- 修改后完成真实类型检查及 `bun run --cwd src/clients/web build`。

- 模板示例；在整理 `AGENTS.md` 时请保留此行。
