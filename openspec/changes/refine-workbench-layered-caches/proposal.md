## Why

快速访问变更已扩展到跨层缓存完善，配置、Worker、Webview 与资源持久缓存的剩余验收不应持续推迟快速访问主功能的交付。本变更承接原第 17／18 组，保留现有实现与准确证据，仅推进尚未闭合的缓存纵向链。

## What Changes

- 从 `add-workbench-quick-access` 迁入分层缓存接入、模型共享、派生缓存代际、静态资源和恢复数据保护的后续合同；不重做已接通链路。
- 接通原 Webview 缓存、普通字节读取与 Web／打包 Electron 的持久复用，沿各原 owner、标准 HTTP／Cache Storage 和原权限校验；不增加通用缓存引擎、字节库或 native IPC。
- 保留原任务 17／18 编号及已完成项；部分收据不代替未完成场景，不把新规划标记为实现。
- 独立交付，不作为快速访问的反向依赖；身份隔离、原查询缓存和既有编辑器／草稿恢复仍由快速访问当前消费者验收。

## Capabilities

### New Capabilities

- `workbench-cache-lifecycle`: 原服务的分层缓存、共享释放、代际失效和受保护恢复数据边界。

### Modified Capabilities

- `resource-file-operations`: 在快速访问已定义的原文件私有条件读取基础上，补原 Webview 加载链和跨 Web／Electron 的持久字节复用；不改变 URI、保存 CAS 或下载／Range 边界。

## Impact

影响已有 Workbench 文件 Provider、模型引用、Webview 宿主、配置与静态资源缓存及相应定向测试。依赖快速访问的原文件条件读取合同，复用其它变更的稳定 owner 与原恢复机制；不复制搜索、安装、配置或恢复的权威状态，不增加服务和运行环境设置。
