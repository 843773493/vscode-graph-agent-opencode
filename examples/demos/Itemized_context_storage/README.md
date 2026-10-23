# Itemized Context Storage Demo

这是当前主仓库 item 化模型上下文的最小测试与教学示例。它不导入主仓库运行时代码；demo 生成的 JSONL、SQLite、计划和投影位于本目录的 `runtime/demo/`，测试产物单独写入仓库根目录的 `out/tests/`。

## 运行

在本目录执行：

```bash
bun run demo
bun run start
```

然后打开 `http://127.0.0.1:8142`。端口可用跨平台参数 `bun src/server.js --port 8143` 覆盖；也继续支持 `ITEMIZED_CONTEXT_PORT` 环境变量。

也可以直接查看 JSON：

```bash
bun src/cli.js --json
```

## 这份快照展示什么

```text
runtime/demo/
├── sessions/session-itemized-teaching/rollout/rollout.jsonl
├── sessions/session-itemized-teaching/rollout/index.sqlite
├── request-plan.json
└── transcript.json
```

- `rollout.jsonl` 是 canonical item 的 append-only 正文源，一行一个 item。每个 item 保留自己的 identity、semantic kind、payload、producer 和 content hash。
- `index.sqlite` 是查询和视图状态：`item_catalog` 保存 JSONL offset/length 与 item 元数据，`item_projections` 保存有限预览，`context_views` 保存 active view 的 included/omitted 结果。SQLite 不重复保存 canonical payload。
- `request-plan.json` 展示一次 Provider 请求如何同时引用 canonical history 和 request-only contribution。system 指令、tool set 定义只属于本次请求，不回写 `rollout.jsonl`。
- `transcript.json` 是用户界面的粗粒度投影，只显示 user/assistant 文本，不能代替模型上下文。

`bun test` 会为每次运行创建独立目录 `out/tests/examples/demos/Itemized_context_storage/tests/storage.test/workspace/<run_id>/runtime/`，关闭 SQLite 后保留 JSONL、数据库和投影供复查；`bun run demo` 只重置 `runtime/demo/`。
