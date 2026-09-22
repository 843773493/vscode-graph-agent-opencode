# 目录用途

`src/clients/web/src/utils/media/` 存放浏览器前端的“媒体附件”纯工具：一类把上传/粘贴进来的 `File` 判定为图片/视频/通用文件并构造 composer 可用的 `SelectedAttachment`（含 data URL 归一与兜底文件名/类型）；另一类把后端 `AttachmentRef` 归一为消息渲染用的媒体项与媒体类型（image/audio/video/file）。

与相邻文件的边界：

- `utils/fileTransferHost.ts`：工作区文件下载执行器（`FileTransferHost.downloadWorkspaceFile` 以 anchor 触发浏览器原生导航），服务于 `api/workspaceFilesystem.ts` 的下载链路，不含任何媒体类型判定，**不属于本包**。
- `utils/selection/`：浏览器元素选择与工作区目录选择工具，与媒体附件无关。
- `utils/markdown.ts`、`utils/workspaceMarkdown.ts`、`utils/skillMarkdown.ts`：Markdown 渲染与解析族，与媒体附件无关。
- `types/`：后端协议类型（如 `AttachmentRef`）的权威来源，本包只消费不定义。

# 可修改内容

- 附件媒体类型判定（图片/视频/文件）与视频扩展名/内容类型白名单。
- `File` 到 `SelectedAttachment` 的构造：data URL 内容类型归一、文件名与文件 id 兜底、剪贴板文件提取。
- 后端 `AttachmentRef` 到 `MessageMediaItem` 的映射与媒体类型归一。
- 本包内部的模块拆分与符号导出调整。

# 不可修改内容

- 不放 React 组件，不发起 HTTP 请求，不读写业务状态。
- 不实现工作区文件下载/传输（那属于 `utils/fileTransferHost.ts` 与 api 下载链路）。
- 不定义后端协议类型的权威来源；协议类型只能来自 `src/clients/web/src/types/`。
- 不在工具函数中静默吞掉错误；遇到不合法输入（如非法 data URL）时应暴露明确错误。

# 规范

- 工具函数保持输入输出明确，避免依赖全局可变状态；仅在职责确需时使用浏览器对象（`FileReader`、`crypto`、`DataTransfer`）。
- 媒体类型判定与兜底策略属于用户可见契约，新增或修改判定分支必须同步对应测试。
- 导入上级共享模块使用显式相对路径（如 `../../types/backend`），禁止任何指向旧平铺路径的转发层。
- 代码注释使用中文，专业术语除外。
- 修改本目录后需要运行前端静态检查和 `bun run build`。
- 模板示例；在整理 `AGENTS.md` 时请保留此行。
