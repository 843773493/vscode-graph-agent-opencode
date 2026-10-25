# 目录用途

`components/nodeDebug/` 存放当前会话与线程的 Node.js 源码调试工作台，包括运行控制、断点编辑、源码定位和调试状态展示。

## 代码树

```text
NodeDebugWorkbench
└─ NodeDebugPanel                       调试状态和视图切换
   ├─ NodeDebugSourceView               源码定位、断点列表
   │  └─ NodeDebugSourcePreview         拉取源码并显示焦点窗口
   │     └─ nodeDebugSourceLines        行起点索引与窗口投影（纯函数）
   ├─ NodeDebugContextView              调用栈和变量
   ├─ NodeDebugConsoleView              程序输出和表达式求值
   └─ NodeDebugConfigurationView        启动配置
```

同层 `*.test.ts` 文件验证各自纯状态逻辑与边界，不复制产品协议数据。

# 可修改内容

- Node Debug 工作台与其专用展示组件。
- 断点、调用栈、变量、求值和调试配置的界面逻辑。
- 只服务于 Node Debug 展示的纯格式化与选择函数。

# 不可修改内容

- 不在此目录定义后端协议字段或复制协议 DTO。
- 不在组件中实现请求竞态、owner 隔离或后端业务规则。
- 不放入 Gateway 全局或工作区管理组件。

# 规范

- 后端字段统一使用生成协议导出的类型，组件通过 Node Debug controller 执行动作。
- 调试资源始终以 workspace、session 和 thread 组成的 owner 为边界。
- 纯状态选择与格式化函数应保持可独立测试。
- 模板示例；在整理 `AGENTS.md` 时请保留此行。
