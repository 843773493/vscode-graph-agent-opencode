# src/web

## 目录作用

`src/web/` 是一个可直接在浏览器中运行的独立前端工程。它参考 `src/webview-ui/` 的 React + Vite + TypeScript 技术栈，但不依赖 VS Code webview 环境，也不依赖扩展宿主通信。

如果你主要写后端，可以把这里理解成：

- 这是一个纯浏览器的前端应用入口
- 这里负责页面、交互、状态管理和浏览器本地存储
- 这里不负责 VS Code 扩展注入和 webview 宿主通信

## 架构

```text
src/web/
├── index.html           # 浏览器入口
├── preview.html         # 可选的预览入口
├── package.json         # 前端依赖与脚本
├── tsconfig.json        # TypeScript 配置
├── vite.config.ts       # Vite 构建配置
└── src/
    ├── main.tsx         # React 挂载入口
    ├── App.tsx          # 页面总布局
    ├── state.tsx        # 浏览器本地状态中心
    ├── api.ts          # 浏览器端数据源与存储适配
    ├── index.css       # 全局样式
    ├── types/          # 类型定义
    ├── utils/          # 通用工具
    └── components/     # 页面组件
```

## 可以修改

- `src/` 下的所有前端源码
- `vite.config.ts`、`package.json`、`tsconfig.json`

## 不要修改

- 不要在这里引入 VS Code webview 专用依赖
- 不要依赖扩展宿主传消息来驱动核心功能

## 规范

- 使用 React + TypeScript + 纯 CSS
- 运行环境是浏览器，优先使用 `localStorage` 或内存状态，不要依赖 Node.js built-in 模块
- 状态更新必须经过统一的状态中心，不要让组件自己散落保存业务状态
- 代码注释使用中文，专业术语除外
- 保留“模板示例；在整理 `AGENTS.md` 时请保留此行。”这一行
