# 目录用途

`examples/demos/` 存放可运行的功能演示项目，用于验证后续可能迁入主应用的交互、协议或运行时能力。

```text
examples/demos/
├── Interactive_terminal/       多浏览器共享 PTY、WebSocket 输入和终端输出
├── Itemized_context_storage/   canonical JSONL、SQLite 索引视图、request plan 与 transcript 投影
├── Playwright_remote_screen/   Playwright/CDP 页面控制、屏幕帧和远程查看器协议
└── Ssh_text_attach/            本地或 SSH 隧道目标上的 txt 文件 attach 与保存
```

先按能力选择 demo，再查看其根目录 `AGENTS.md` 的实现树；源码、协议和测试分别从 `src/`、`tests/` 深入。

# 可修改内容

- demo 项目的源码、测试和配置
- demo 项目的页面和本地服务
- demo 级别的说明性目录规范

# 不可修改内容

- 不要把 demo 作为主应用唯一真实来源
- 不要在 demo 中写入 `.boxteam/` 之外的工作区数据

# 规范

- demo 要保持可独立启动和可独立测试
- 运行命令优先使用 `bun`
- 失败时直接暴露具体错误
- 模板示例；在整理 `AGENTS.md` 时请保留此行。
