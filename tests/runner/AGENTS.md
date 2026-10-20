# 目录用途

存放测试套件注册、能力矩阵和统一命令入口，不包含具体测试实现。

## 可修改内容

- `matrix.jsonc`、ESM runner、套件过滤和结构化汇总。

## 不可修改内容

- 不要隐藏失败、吞掉退出码或把未满足前置条件记为通过。
- 不要让 E2E 套件回退执行 Integration stub 套件。

## 规范

- 所有 JavaScript 使用 ESM 并通过 Bun 执行。
- 矩阵显式声明层级、客户端、运行面、命令和真实前置条件。
- `unit-workspace-services` 递归发现 Browser、Terminal 的服务端、attach 页面纯逻辑及协议 JS 单测；它不启动实际辅助服务，不能代替真实进程 Integration 验收。
- `run-workspace-services.mjs` 按静态导入的测试框架分组：`node:test` 交给Node原生测试器，`bun:test` 交给Bun。两组都执行并汇总失败，未知或混用框架在启动前拒绝；各进程沿用矩阵的超时与内存保护。测试器的编码等运行时语义须与被测行为匹配，不能用Bun的Node兼容行为代替Node服务端证据。
- `integration-browser-element-references` 使用真实Chromium验证辅助Browser服务的DOM引用与动作，固定60秒/4GiB；不代表Gateway或主Web产品链路验收。
- `integration-browser-checkpoint-form-state` 使用真实Chromium与HTTP表单验证检查点捕获、DOM变更后的定位、释放后恢复及大型匿名表单的扫描成本，固定60秒/4GiB；Manager落盘边界为空操作，不代表持久化或主Web验收。
- 每个已启用 suite 都带数据段内存上限与硬超时；被上限或超时中止的套件必须以 TIMEOUT 或非零退出码报出，不得记成通过。
