# 目录用途

React 产品共享的确认 Provider、错误边界、启动骨架和内容槽编排。导航、Tab、Grid、底部标签、布局和设置由原版 Code OSS 工作台管理。

# 可修改内容

- WarmConfirmProvider、AppErrorBoundary、BootstrapState、ContentViewSlots 及其测试。

# 不可修改内容

- 不恢复 Toolbar 或 WorkbenchBottomPanel 等旧 App 外壳。
- 不实现业务面板内部规则，不伪造成功或吞掉错误。

# 规范

- 内容槽的懒加载与隐藏保留沿用既有行为。
- ReactSurface／双根与主题隔离由正式组件浏览器场景验证；完整失败恢复由真实 Gateway 页面验证。
- 修改后完成相关测试、真实类型检查及 `bun run --cwd src/clients/web build`。

- 模板示例；在整理 `AGENTS.md` 时请保留此行。
