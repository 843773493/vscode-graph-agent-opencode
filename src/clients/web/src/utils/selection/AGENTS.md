# 目录用途

`src/clients/web/src/utils/selection/` 存放浏览器前端的“选择结果”解析与工作区路径工具：一类把扩展窗口浏览器区域回传的元素选择消息解析、格式化并交给 composer 消费；另一类提供工作区目录选择弹窗使用的路径规范化与树状搜索匹配。

与相邻文件的边界：

- `utils/jsonDisplay.ts`：提供全前端共享的 `isRecord` 权威实现（非数组的 object）。本包 `browserElementSelection` 内的 `isRecord` 刻意接受数组，仅服务元素选择校验，语义与前者不同，不得合并。
- `utils/markdown.ts`、`utils/workspaceMarkdown.ts`、`utils/skillMarkdown.ts`：Markdown 渲染与解析族，与选择结果无关。
- `utils/fileTransferHost.ts`、`utils/mediaAttachments.ts`、`utils/messageMedia.ts`：附件与文件传输族，与元素/目录选择无关。

# 可修改内容

- 浏览器元素选择消息（`boxteam:browser-element-selected`）的解析、校验与文案格式化。
- 工作区目录选择路径的规范化、父目录推导、查询匹配与树状搜索过滤。
- 本包内部的模块拆分与符号导出调整。

# 不可修改内容

- 不放 React 组件，不发起 HTTP 请求，不读写业务状态。
- 不定义后端协议类型的权威来源；协议类型只能来自 `src/clients/web/src/types/`。
- 不把本包内收录的元素选择数据改写成后端权威数据，也不在本包实现会话或目录业务规则。

# 规范

- 纯函数优先，输入输出显式，非法输入必须抛出明确错误或按既定边界收敛。
- 选择结果属于用户可见契约，新增或修改校验分支与文案必须同步对应测试。
- 导入其他模块时使用显式相对路径，禁止任何指向旧平铺路径的转发层。
- 代码注释使用中文，专业术语除外。
- 修改本目录后需要运行前端静态检查和 `bun run build`。
- 模板示例；在整理 `AGENTS.md` 时请保留此行。
