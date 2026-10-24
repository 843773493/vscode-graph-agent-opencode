# 目录用途

存放浏览器前端可复用的纯工具函数、格式化逻辑和与 UI 框架无关的小型浏览器辅助能力。

## 组织结构

```text
utils/
├── abortable.ts                 可取消的异步等待
├── attachUrls.ts                Gateway 资源附加链接
├── clipboard.ts                 剪贴板读写
├── displayText.ts               展示文本清理
├── errorMessage.ts              未知错误转展示文案
├── extensionResourceWindow.ts   扩展窗口入口与 URL
├── fileTransferHost.ts          工作区文件下载宿主
├── format.ts                    日期、数字等展示格式
├── jsonDisplay.ts               JSON 展示共用判定
├── pointerDrag.ts               DOM 指针拖拽生命周期
├── workspaceFileReferences.ts   工作区文件引用文本及后端已校验 URI 的目录根拆分
├── markdown/                    Markdown 渲染与链接目标解析
├── media/                       媒体附件构造与归一
└── selection/                   浏览器元素和工作区目录选择
```

工具文件的测试与实现并排放置，使用同名 `.test.ts`。新工具先按职责归入上表：Markdown 文本解析放 `markdown/`，媒体类型和附件映射放 `media/`，用户选择结果与路径搜索放 `selection/`；下载执行、工作区引用文本、剪贴板和通用格式工具留在顶层。只有形成职责清楚、可复用的一组文件时才新增子目录，并为新源码目录补齐 `AGENTS.md`。

# 可修改内容

- 新增或调整格式化、解析、附件处理、Markdown 处理等无状态工具函数。
- 放置可被多个组件或状态模块复用的轻量类型和常量。

# 不可修改内容

- 不在本目录保存业务状态或实现后端状态同步规则。
- 不在本目录直接渲染 React 组件。
- 不在工具函数中静默吞掉错误；遇到不合法输入时应暴露明确错误。

# 规范

- 工具函数应尽量保持输入输出明确，避免依赖全局可变状态。
- 仅在浏览器 API 是函数职责的一部分时使用浏览器对象。
- 修改本目录后需要运行前端静态检查和 `bun run build`。
- 模板示例；在整理 `AGENTS.md` 时请保留此行。
