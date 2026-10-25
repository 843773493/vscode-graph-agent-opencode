# 目录用途

`src/clients/web/src/utils/markdown/` 只存放工作区 Markdown 链接与图片引用目标解析工具，负责区分工作区相对路径与外部 URL。Markdown 展示由对应的 React renderer 负责：聊天消息使用 `components/chat/MarkdownContent.tsx`，工作区文件预览使用 `components/workspace/preview/WorkspaceMarkdownRenderer.tsx`。

## 职责树

```text
markdown/
├── workspaceMarkdown.ts        工作区 Markdown 链接/图片目标解析
└── workspaceMarkdown.test.ts   相对路径、外部 URL 与路径边界合同
```

按处理目标选择入口：需要将工作区 Markdown 中的相对链接或图片引用解析到工作区资源时使用 `workspaceMarkdown.ts`。React 组件负责渲染与交互，本目录不提供第二套 HTML renderer。

与相邻文件的边界：

- `utils/media/`：媒体附件类型判定与 `SelectedAttachment` 构造，与 Markdown 文本无关。
- `utils/selection/`：浏览器元素选择与工作区目录选择，与 Markdown 无关。
- `utils/workspaceFileReferences.ts`：工作区文件引用文本处理，与 Markdown 链接解析职责不同，不并入。
- `types/`：后端协议类型的权威来源，本包只消费不定义。

# 可修改内容

- 工作区 Markdown 链接/图片目标的解析、相对路径归一与工作区归属判定。
- 本包内部的模块拆分与符号导出调整。

# 不可修改内容

- 不放 React 组件，不发起 HTTP 请求，不读写业务状态。
- 不定义后端协议类型的权威来源；协议类型只能来自 `src/clients/web/src/types/`。
- 不在工具函数中静默吞掉错误；遇到不合法输入时应暴露明确错误或按既定边界收敛。

# 规范

- 纯函数优先，输入输出显式，避免依赖全局可变状态。
- 链接/图片目标解析属于用户可见契约，新增或修改解析分支必须同步对应测试。
- 导入上级共享模块使用显式相对路径（如 `../../types/backend`），禁止任何指向旧平铺路径的转发层。
- 代码注释使用中文，专业术语除外。
- 修改本目录后需要运行前端静态检查和 `bun run build`。
- 模板示例；在整理 `AGENTS.md` 时请保留此行。
