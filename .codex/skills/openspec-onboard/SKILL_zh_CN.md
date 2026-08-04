---
name: openspec-onboard
description: 以中文引导用户完成第一个 OpenSpec 全流程，在真实代码库中边讲解边完成探索、规划、实施和归档。
allowed-tools: Bash(openspec:*)
license: MIT
metadata:
  author: openspec
  version: "1.0-zh-cn"
---

# OpenSpec 中文入门引导

带用户在真实代码库中完成一次完整变更周期，并在关键节点解释工作流。

**Store 选择：**用户指定 store 时运行 `openspec store list --json`，并为支持的命令添加 `--store <id>`；否则使用最近的本地 OpenSpec 根目录。

## 预检

运行 `openspec --version`。未安装时明确提示先安装，并停止。

## 阶段 1：欢迎

说明将完成：选择小型真实任务、简短探索、创建 change、生成 proposal → specs → design → tasks、实施并归档；预计约 15–20 分钟。

## 阶段 2：选择任务

扫描小型改进机会：TODO/FIXME/HACK/XXX、吞掉异常、缺测试函数、TypeScript `any`、遗留 debug 语句、缺少输入校验；同时查看最近 10 条 git 记录。

给出 3–4 个具体建议，包含文件位置、预计范围和适合作为入门任务的理由，并允许用户自选。没有明显机会时直接询问用户。

任务过大时温和建议缩小切片或另选；用户坚持时尊重选择。

## 阶段 3：探索演示

读取相关代码，做 1–2 分钟简短分析，必要时画 ASCII 图，说明 `/opsx:explore` 用于实施前或实施中的思考。**暂停并等待用户确认。**

## 阶段 4：创建变更

解释 change 是承载 proposal、spec、design 和 tasks 的规划容器。推导 kebab-case 名称，运行 `openspec new change "<name>"`，再用 status JSON 获取真实 `changeRoot`，展示目录结构。

## 阶段 5：Proposal

解释 proposal 描述为什么做以及高层范围。依据任务起草 Why、What Changes、Capabilities、Impact，但先不保存。**暂停等待用户确认。**确认后运行 `openspec instructions proposal --change "<name>" --json`，写入其 `resolvedOutputPath`。

## 阶段 6：Specs

解释 spec 用可测试语言定义构建内容。运行 `openspec instructions specs --change "<name>" --json`；若输出为 glob，根据 schema 指令选择具体 capability 路径。用 `### Requirement:` 和 `#### Scenario:`，以及 WHEN/THEN/AND 编写并保存。

## 阶段 7：Design

解释 design 记录技术方案与取舍。起草 Context、Goals/Non-Goals 和 Decisions；小变更可以简短。写入 design 指令返回的 `resolvedOutputPath`。

## 阶段 8：Tasks

将 spec 与 design 拆为小而清晰、顺序合理的复选框任务，并包含验证步骤。展示草稿，**暂停等待用户确认实施**，随后写入 tasks 指令返回的路径。

## 阶段 9：实施

逐项宣布任务、修改真实代码、自然说明 spec/design 如何约束实现，并立即把 `- [ ]` 更新为 `- [x]`。讲解保持简洁。全部完成后展示清单。

## 阶段 10：归档

解释归档保存决策历史，运行 `openspec archive "<name>"`，展示基于 `planningHome.changesDir` 的真实归档路径。

## 阶段 11：回顾

回顾 Explore、New、Proposal、Specs、Design、Tasks、Apply、Archive，并介绍 `/opsx:propose`、`/opsx:explore`、`/opsx:apply`、`/opsx:archive`、`/opsx:new`、`/opsx:continue`、`/opsx:ff`、`/opsx:verify`。

## 中途退出

用户暂停时说明工作保存在 status 返回的 `changeRoot`，可用 `/opsx:continue <name>` 恢复，tasks 已存在时可用 `/opsx:apply <name>` 实施。用户只想看命令时提供简明命令表后结束，不施压。

## 约束

- 关键转换遵循“解释 → 执行 → 展示 → 暂停”。
- 实施时轻量讲解，不逐行授课。
- 入门流程不跳阶段；标记处等待确认但不过度暂停。
- 必须使用真实代码任务，不使用虚构演示。
- 温和控制范围，同时尊重用户最终选择。
