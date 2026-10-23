# 目录用途

存放输入框（Composer）相关的 hook：草稿持久化、斜杠命令解析与执行、Composer 状态投影，以及 Composer React 上下文边界测试，负责把输入框草稿、命令与上下文状态整理成可复用的副作用单元。

## 职责树

```text
composer/
├── useComposerDraft.ts                    草稿作用域、持久化与超限内存态
├── useComposerDraftOverflow.test.tsx      超限草稿仍可编辑且不写入 localStorage
├── useComposerSlashCommands.ts            斜杠命令副作用与结果提示
├── useComposerSlashCommands.test.tsx      命令识别、执行和结果处理
├── useComposerStateProjection.ts          AppState 到 Composer 状态快照的投影
├── useComposerStateProjection.test.tsx    快照身份、会话隔离和更新边界
├── useSkillUntrackCommand.ts              停止跟踪 Skill 的后端命令
├── useSkillUntrackCommand.test.tsx        命令回执与失败状态
└── composerContext.test.tsx               Composer React 上下文和状态边界
```

按副作用链路选择实现：草稿值与作用域持久化走 `useComposerDraft`，纯草稿存储规则留在 `state/composerDrafts/`；斜杠命令的匹配规则留在 `state/slashCommands`，执行副作用走 `useComposerSlashCommands`；完整应用状态到输入框上下文的只读投影走 `useComposerStateProjection`。Skill 停止跟踪属于独立后端动作，走 `useSkillUntrackCommand`，不要塞进通用斜杠命令处理器。

# 可修改内容

- Composer 草稿的读写、作用域键与持久化。
- 斜杠命令的匹配、执行与结果处理。
- Composer 上下文状态快照的选取与投影。
- Composer React 状态边界的测试。
- 与上述逻辑直接相关的纯函数测试。

# 不可修改内容

- 不定义后端协议类型的权威来源，不实现 UI JSX 组件。
- 不吞掉输入或命令执行错误；错误必须写入状态或继续抛出。
- 不伪造草稿或命令状态，成功状态只来自后端返回值与本地权威存储。

# 规范

- 草稿与命令逻辑优先复用 `state/composerDrafts/`、`state/slashCommands`、`state/composerState` 下的纯函数。
- 导入其他 hook 或共享模块时使用显式相对路径，禁止任何指向旧平铺路径的转发层。
- 代码注释使用中文，专业术语除外。
- 修改本目录后需要运行前端静态检查和 `bun run build`。
- 模板示例；在整理 `AGENTS.md` 时请保留此行。
