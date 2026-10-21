# 目录用途

`packages/launcher/src/` 存放 Launcher 的可测试实现模块，按职责分成配置解析、联邦凭证、进程生命周期三组。

```text
src/
├── cli.mjs                  命令入口与顶层分发
├── doctor.mjs               诊断结果构建与呈现
├── configuration/           配置安装、运行时发现与 manifest 校验
├── federation/              Gateway 联邦配对凭证
└── process/                 detached 进程、Gateway 监督、实例锁、日志与停止
```

每组的实现和单元测试并列放置；新增文件先归入对应职责目录，不建立旧路径转发层。

# 可修改内容

- 可以实现 manifest 校验、进程监督、锁和诊断 helper。

# 不可修改内容

- 不依赖仅在源码 checkout 存在的相对目录。
- 不静默修复损坏 manifest 或切换系统运行时。

# 规范

- 模块抛出包含资源路径和发行标识的明确错误。
- 顶层入口保持轻量，复杂行为拆到聚焦模块。
- 模板示例；在整理 `AGENTS.md` 时请保留此行。
