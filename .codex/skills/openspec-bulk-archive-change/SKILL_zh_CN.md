---
name: openspec-bulk-archive-change
description: 一次归档多个 OpenSpec 变更，并通过检查实际代码智能解决 spec 冲突。用户希望批量归档并使用中文工作流时使用。
allowed-tools: Bash(openspec:*)
license: MIT
metadata:
  author: openspec
  version: "1.0-zh-cn"
---

# 批量归档 OpenSpec 变更

**输入：**无需名称，始终让用户多选。指定 store 时先解析 ID，并为支持的命令添加 `--store <id>`。

## 步骤

1. 运行 `openspec list --json` 获取活跃变更；没有变更时告知并停止。
2. 让用户多选变更，显示每项 schema，并提供“全部变更”。不得自动选择；允许选择 1 个或更多。
3. 对每个选中变更收集：
   - `openspec status --change "<name>" --json` 返回的 schema、artifact、路径和作用域。
   - 从 `artifactPaths.tasks.existingOutputPaths` 统计任务完成度；无文件则标记“无任务”。
   - 从 `artifactPaths.specs.existingOutputPaths` 列出 capability 与 requirement 名称。
4. 建立 `capability -> changes` 映射；同一 capability 被两个以上变更触及时视为冲突。
5. 逐个解决冲突：读取各 delta spec，在代码和测试中寻找真实实施证据。
   - 只有一个变更已实施：只同步该变更。
   - 多个都已实施：按创建时间从旧到新合并，新变更优先。
   - 都未实施：跳过同步并警告。
   记录顺序、依据与代码证据。
6. 展示统一状态表：Change、Artifacts、Tasks、Specs、Conflicts、Status；列出冲突处理和未完成警告。
7. 只请求一次批量确认，提供“全部归档”“仅归档已就绪项”“取消”等与当前状态匹配的选项。
8. 按确定顺序执行：先用 `openspec-sync-specs` 的智能合并方法同步，再将 `changeRoot` 移到 `<planningHome.changesDir>/archive/YYYY-MM-DD-<name>`。
9. 逐项记录成功、跳过或失败。某个目标已存在时只让该项失败，继续其余项。
10. 汇总归档路径、跳过项、失败原因、同步数量和冲突解决情况。

## 约束

- 始终由用户选择并只做一次整体确认。
- 归档前尽早检测 capability 冲突，并依据实际代码解决。
- 保留 `.openspec.yaml`。
- 不覆盖已有归档目录；单项失败不得中断其余项。
- 所有成功、跳过与失败都必须明确报告。
