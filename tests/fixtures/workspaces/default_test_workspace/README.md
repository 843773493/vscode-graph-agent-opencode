# 统一测试工作区

这个目录是仓库测试共用的只读 TypeScript 工作区模板，由 `tests/support/workspaces.py` 复制到各测试的 `out/tests/` 目录后使用。

它用于演示一条完整的 agent 工作流：

- `read` 读取上下文
- `grep` / `glob` 找到相关文件
- `lsp` / diagnostics 反馈修改结果
- `edit` / `patch` / `multiedit` 执行修复
- `snapshot_create` / `snapshot_restore` 做撤销
- watcher 或诊断结果回传给 agent 继续迭代

建议优先查看这些文件：

1. `test.md`
2. `src/index.ts`
3. `src/context.ts`
4. `src/editor.ts`
5. `src/state.ts`
