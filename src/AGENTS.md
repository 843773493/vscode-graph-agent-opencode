# src

## 目录作用

VS Code 扩展的客户端源代码根目录。扩展入口 `extension.js` 在此，后端管理器、共享模块、Webview 侧边栏三部分分别位于三个子目录中。

## 架构

```
src/
├── backend/        后端进程管理：启动/探测/健康检查/优雅关闭本地 FastAPI 后端
├── shared/         共享模块：API 客户端、常量、Webview-Host 通信协议
├── webview/        Webview 侧边栏：HTML 模板、前端交互、扩展 Host 端状态管理
└── extension.js    扩展入口：注册命令、视图、状态栏；不包含业务逻辑
```

## 可以修改

- 所有文件，遵循各子目录的约定
- 新增子目录或文件

## 不要修改

- 不要在此目录存放后端（Python）代码，后端代码在项目根目录的 `app/` 下
- 不要在此目录存放构建配置文件

## 约定

- 所有 JS 代码使用 ESM（`import`/`export`），避免 CommonJS
- 扩展入口 `extension.js` 只做注册和编排，不包含业务逻辑
- 各子目录的职责边界清晰，不要跨职责引用（除 `shared/` 外）