# 目录用途

`components/composer/` 存放消息输入框及其附件、视图、Agent、工具选择和发送控制组件。

## 支撑树

```text
composer/
├── Composer.tsx              输入、附件、发送与会话动作编排
└── composerTestFixtures.ts   Composer hook 测试共享的浏览器全局环境
```

Composer 专属的 API fake、数据装配和行为断言留在相邻测试文件；全局 window、存储、浮层构造器、BroadcastChannel 和可控轮询计时器由共享 fixture 安装并按描述符还原。

# 可修改内容

- 可以调整输入框局部交互、工具栏控件、附件展示和工具选择浮层。
- 可以继续拆分只服务于 Composer 的展示组件。

# 不可修改内容

- 不在组件中实现后端工具测试规则或持久化业务。
- 不在这里放置会话历史、文件预览或工作区侧栏组件。

# 规范

- 后端状态通过 `api.ts` 或上层 App 状态访问，失败必须显示给用户。
- 浮层需支持键盘关闭、视口边界约束和窄屏操作。

