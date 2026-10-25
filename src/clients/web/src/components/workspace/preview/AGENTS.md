# 目录用途

`components/workspace/preview/` 存放工作区预览区的专用内容渲染组件。

```text
preview/
├── WorkspaceMarkdownPreview.tsx      文档上限提示与 renderer 延迟加载
├── WorkspaceMarkdownRenderer.tsx      Markdown/GFM 解析、链接、图片与 Mermaid 投影
├── WorkspaceMarkdownImage.tsx         工作区图片请求、外链图片原生懒加载
├── WorkspaceAttachmentPreview.tsx     会话附件预览与对象 URL 生命周期
└── MermaidDiagram.tsx                 Mermaid 懒加载与图表渲染
```

工作区 Markdown 图片在进入视口附近后才读取文件 Blob；组件卸载时中止未完成请求并回收对象 URL。外链图片交给浏览器的 `loading="lazy"`，不经过工作区文件 API。

# 可修改内容

- Markdown、Mermaid、媒体等文件预览 renderer 及其局部展示状态。

# 不可修改内容

- 不直接实现后端协议、工作区路径安全规则或标签持久化。
- 不保存跨标签的业务状态。

# 规范

- renderer 通过 props 接收资源内容和动作，不直接依赖应用级状态。
- 用户可见错误必须明确展示，不得静默忽略。
