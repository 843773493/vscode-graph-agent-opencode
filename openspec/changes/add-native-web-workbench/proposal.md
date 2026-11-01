## Why

当前 Web 工作台手工组织布局和会话选择，无法直接提供 VS Code 的完整编辑器组、标签、命令和设置行为。引入独立的完整 Code OSS Web 客户端，以原版工作台承载多 Thread，并保留现有后端协议。

## What Changes

- 新增独立 Workbench 客户端，固定官方源码版本并纳入 Git，构建和样式不依赖现有 React Web 入口或 reference_repo。
- 先完整启动原版 Web Workbench，用普通文件验收预览/固定标签、分屏、命令面板、设置与恢复。
- 再接入 Gateway 会话导航与精确 Thread 编辑器，将会话资源绑定在编辑器内部；不改变后端业务权威来源。
- 迁移期间独立入口用于验收，替换旧入口须完成全部业务迁移；不增加长期兼容层。

## Capabilities

### New Capabilities

- `native-web-workbench`: 完整原版工作台、独立构建与存储、原生编辑器交互及 Thread 集成。

### Modified Capabilities

无；旧客户端当前契约保持不变，正式替换另以全量迁移验收为准。

## Impact

新增 src/clients/workbench；复用既有 Gateway 与工作区 API。保留 Code OSS 上游许可证与提交身份。测试工作区隔离，8111 保持关闭，使用独立端口验收。桌面宿主功能和微软发行版外部服务不属于 Web 版本原版行为的承诺。
