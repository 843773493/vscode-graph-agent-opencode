# Webview 历史栏关闭后右侧空白问题复盘

## 📅 发生时间
2026-05-31

## ❌ 问题现象
在 `src/webview-ui/dist/preview.html` 中关闭历史栏后，右侧仍然保留一大块空白，看起来主 UI 没有拉伸到整个可视区域。

## 🔍 排查过程

### 第一阶段：误判为内部卡片宽度问题
最初以为是 `chat-panel`、`chat-stream` 或消息卡片本身没有撑满宽度，所以先调整了这些区域的 `width / flex / max-width`。

### 第二阶段：确认根容器其实已经全宽
通过浏览器实际测量发现：
- `body` 与 `#root` 已经能占满视口
- `app-shell` 也已经全宽

这说明空白不是根容器没撑开，而是更下层的布局状态没有切对。

### 第三阶段：确认历史栏仍在布局流里占位
在浏览器里继续测量后发现：
- 历史栏看似关闭，但其实还在布局中占着宽度
- `chat-panel` 只是从中间开始排布，右侧自然留下空白

也就是说，问题本质不是“内容没拉满”，而是“历史栏没有真正从布局中移除”。

## ✅ 根本原因
关闭历史栏时，`historyPanelOpen` 的状态虽然存在，但布局没有真正切成单栏模式，导致：

1. `history-panel` 仍然参与 flex 布局
2. `chat-panel` 只能拿到剩余宽度
3. 右侧出现视觉空白

## 🔧 修复方案

### 1. 关闭历史栏时直接隐藏历史栏区域
在 `src/webview-ui/src/index.css` 中补充：

```css
.content-layout.history-closed > .history-panel {
  display: none;
}
```

### 2. 让主内容区真正占满剩余空间
补充单栏模式下的全宽规则：

```css
.content-layout.history-closed .chat-panel {
  width: 100%;
  max-width: none;
  flex: 1 1 auto;
}
```

### 3. 保证根节点全宽
同时修正了 `body > #root` 与 `.app-shell` 的伸展方式，避免根节点按内容收缩。

## 🧪 验证方式
我用浏览器实际打开 `dist/preview.html`，然后：

1. 点击“关闭历史栏”按钮
2. 观察 `history-panel` 是否消失
3. 测量 `chat-panel` 是否从 `x=0` 开始并占满宽度

最终确认：
- `history-panel` 已消失
- `chat-panel` 变成全宽
- 右侧空白被主 UI 填满

## 📌 顺带完成的工作

### 浏览器预览入口
为了便于不启动 VS Code 也能看前端效果，还新增了：
- `src/webview-ui/preview.html`
- `src/webview-ui/src/preview.tsx`

并把它加入 Vite 构建，使 `dist/preview.html` 可直接用浏览器打开。

### Webview HTML 模板拆分
顺带把 `src/webview/html.js` 中的 HTML 字符串拆成了独立模板文件：
- `src/webview/main.html`
- `src/webview/shell.html`

## 🎯 教训总结
1. 只看组件是否渲染出来，不足以判断布局是否正确，必须看实际尺寸。
2. “关闭”状态如果没有真正把元素从布局流里移除，视觉上仍然会占位。
3. 处理 Webview/预览页布局时，根节点、flex 容器、隐藏状态要一起验证，不能只改局部卡片宽度。

## ✅ 结果
该问题已修复，`dist/preview.html` 中关闭历史栏后，右侧空白会被主 UI 正常填满。