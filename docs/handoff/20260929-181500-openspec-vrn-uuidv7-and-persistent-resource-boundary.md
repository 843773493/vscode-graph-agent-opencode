# OpenSpec / VRN / UUIDv7 与持久资源边界交接

- 交接时间：2026-09-29 18:15:00 UTC+8
- 仓库：`/data/hyf/20260629_agent/vscode-graph-agent-opencode`
- 分支：`main`
- 当前 HEAD：`86b62102 修复(测试): runtime manifest 移出 pytest basetemp，避免被并发会话清理删除`
- 本会话起点：`3e99b9e0`（工作树对起点与 HEAD 均干净）

## 本阶段目标与约束

本阶段的主线是：继续审查并收口 OpenSpec，按已裁定方向开始实施；派生子代理时只显式指定
`newapi-local/deepseek-v4.1-flash` 与显式 reasoning effort；主代理只做调度与架构裁定，
不亲自改底层代码；至少保留「一个改动 + 一个独立审查 + 一个架构/屎山审查」的分离。

沿用且必须继续遵守的硬约束：

- 受保护路径全程禁改：`app/gateway/control/generators.py`、
  `app/services/business/session_generation/service.py`、
  `examples/demos/Itemized_context_storage/`。
- 共享工作树下禁用 `--amend`；提交一律用独立索引
  `GIT_INDEX_FILE=/tmp/<任务名>.idx git read-tree HEAD` → 精确路径 `git add` →
  `git commit -m "中文" -- <精确路径>`，提交后 `git show --name-status` 自检并核对祖先链。
- 判工作树一律 `git diff HEAD`，**不要用 `--cached`**；本会话共享 `.git/index` 陈旧过多次，
  修法是 `cp .git/index /tmp/<备份>.idx && git reset`（裸 mixed reset，禁 `--hard`），
  遇 `.git/index.lock` 等待重试，绝不删锁。
- 跑任何测试都要带进程外保护：`timeout <秒> bash -c 'ulimit -d 4194304; exec "$@"' bash <命令>`。
  禁止裸 `pytest` / 裸 `bun test`（2026-09-27 实测有测试会紧循环把 16GB 吃爆 OOM）。
- 禁 `bun run dev`，禁占用 8010-8032。shell 命令禁反引号与 `$`。
- 临时产物只写 `out/tests/temp/<task_name>/`；探针一律在 `git archive HEAD` 解包的隔离副本里做。

## 已完成提交（按主题分组）

### 领域子包归位与冗余折叠

- `f5c4c496` 下沉 `prefix_epoch`/`root_compilation` 到 `app/domain/itemized/epoch/`，
  顶层 `.py` 由 17 → 15；新增四段式 `epoch/AGENTS.md`。
- `8a28d799` 折叠 7 处重复的 `_non_empty_string`/`_non_empty_str` 到
  `app/domain/itemized/validation.py` 单一定义。

### VRN kind 闭集与 config 来源 VRN 迁移

- `f605ce37` 语法闭集 `_RESOURCE_KINDS` 扩为 `agent-spec | skills | config | session`；
  新增统一构造函数 `resource_display_uri(*, scope, scope_id, kind, tail_segments)`。
  **描述符闭集 `_DESCRIPTOR_KINDS` 保持 `agent-spec`/`skills` 不扩**（两个闭集独立）。
- `838e7110` 勾选 `add-unified-virtual-resource-addressing` 的 1.5 与 5.5 并附代码证据。
- `245e8fff` 定稿 config VRN 的尾段形态：**取逻辑资源名，不取原始文件名**。
  原因是 VRN 动态段闭合 charset 为 `[A-Za-z0-9_-]` 且规范明文禁放宽，
  而真实文件名 `workspace_inline.jsonc` 含 `.`，实测以 `invalid_character` 被拒。
- `50bffa45` `ConfigSource.path: Path` → `ConfigSource.vrn: str | None`；
  workspace / gateway 两侧 `config_source_layers` 与 `config_source_journal`
  物理删除 `source_path`/`backup_path` 列、新增 `vrn` 列。只有 `inline` 层可寻址
  （`scope_id` 取真实 `distribution_id`）；`user`/`user_local`/`workspace`/`sqlite`
  一律 `vrn=None`。真实路径只在读取调用栈内出现。

### 持久资源 real path 收紧

- `f6070abd` 终端与浏览器 owner 持久记录不再落盘 real path：终端改存工作区内相对目录
  `cwd_relative`，浏览器下载/截图改由 `(browser_id, download_id, filename)` 调用栈内重推导；
  移除记录与状态文件里的绝对路径字段与顶层 `workspace_root`。
- `7afb3b65` 消除 real path 上浮：投影层 `cwd`/`checkpoint` metadata 收窄为
  `cwd_relative` 与显式布尔 `checkpoint_available`；截图改经只读端点
  `/api/browsers/{id}/screenshots/{id}` 提供 `screenshot_id` + `screenshot_url`；
  前端 `isRecoverableBrowser` 判据改读显式布尔。
- `bec24a76` 旧格式 `terminals.json` 一次性迁移。**这一笔是本轮最重要的修复**：
  前一版删字段后，真实存量 `terminals.json`（`/home/hyf/.boxteams/boxteam_workspace/.boxteam/terminal-manager/terminals.json`，
  371 KB、18 条终端、只有 `cwd`）会让 `TerminalManager.init()` 抛错，
  而 `backend.js` 的 `await manager.init()` 在 `main()` 顶层 → 终端后端进程直接退出。
- `bbe2eced` 恢复 spec 原文（「凡记录需要表达资源所在位置，该位置 MUST 以 VRN 表达」），
  把 D-A2/D-A3 改为**平行追加**而非替代，并新增唯一一处窄口径 carve-out：
  owner 自身运行态字段（典型即终端 shell 当前工作目录）不是资源定位，按工作区内相对路径持久化。
- `c2f6c844` 关闭三处残余泄漏：`exec_command` 工具结果、`/api/terminals` 与 WS 快照、
  截图端点错误回吐底层异常原文。

### OpenSpec 新增与台账修正

- `b06bc18b` 新增 change `migrate-identifiers-to-uuidv7`（UUIDv4 → UUIDv7）。
- `59070f6b` 落地该 change 的三条 owner 裁定（哈希/幂等键审计作为迁移阻断前置、
  gateway 控制面库逐表分类且失效必须显式报告、在途 v4 文本只点名不代改）。
- `cc27de87` 收口在途 change 的 14 处 UUIDv4 文本为具名引用（点名之外另找全 6 文件 10 处）。
- `b232542d` 删除 `gateway_snapshot` 的「未来重新登记」预留挂点。
- `b1e66a7b` 修正 6 处已过期的 AGENTS.md 路径登记。
- `3980b535` 登记 7.5-F 的二级子目录大文件漏登
  （`storage/catalog/items.py`=880、`turn_projection_reads.py`=802）。

### 测试基建回归修复

- `86b62102` `runtime_manifest_path` 从 pytest basetemp 移到 `out/tests/runtime-manifest/`。
  根因：`pyproject.toml` 的 `tmp_path_retention_policy = "none"` 会让 pytest 把
  共享根 `/tmp/pytest-of-<user>/` 下**每一个** `pytest-<N>` 都收进删除候选并 `rmtree`，
  包括并发会话正在用的那个；全量跑时有嵌套会话退出，删掉了 session 级 manifest，
  导致此后所有触达 config 来源构造的用例撞上 `load_distribution_id` 的 fail-closed。

## 最新验证结果（本会话实测）

- `pytest tests/unit -q -p no:randomly`（带 `ulimit -d 4194304`）：
  **4909 passed, 7 skipped, 0 failed**（1253s）。修复前为 79 failed / 4830 passed。
- `/home/hyf/.bun/bin/openspec validate --strict --all`：**40 passed, 0 failed**。
- `import app.main, app.gateway.main`：EXIT=0。
- 工作树对 HEAD 干净（`git diff HEAD` 为空）。

## 必须保护的工作树改动

本会话结束时工作树对 HEAD 干净；三条受保护路径的既有用户改动状态必须在下轮开工前
用 `git status --short` 与 `git diff --check` 重新确认，不得 reset / restore / 覆盖 / 混入提交。

## 架构残余（本会话观察，未处置）

- `app/services/infrastructure/rollout_context/storage/catalog/items.py`=880、
  `turn_projection_reads.py`=802 已登记；`rollout_maintenance_owner.py`=2336、
  `checkpoint/saver.py`=878、`checkpoint/` 24 个直接 `.py` 均已在 7.5-F 登记但门槛未达标。
- `app/services/infrastructure/rollout_context/` 仍有 34 个 mixin。
- 前端 `App.tsx` / `hooks.tsx` / `state/messageStream.ts` 仍超阈值（见上一份交接）。

## 已识别但未开始的后续切片

1. **VRN `user` scope 未落地**：`add-unified-virtual-resource-addressing` 声称 scope 闭集含
   `user`，而 `grammar.py` 的 `_SCOPE_KEYWORDS` 实测只有 `{workspace, gateway, inline}`。
   `user` 与 `user_local` 的 config 来源在 `50bffa45` 中取 `vrn=None`，依据正是该闭集不含 `user`。
2. **UUIDv7 迁移实施**：`migrate-identifiers-to-uuidv7` 已有完整规划产物，实施前必须先完成
   其 §5A（哈希与幂等键审计）与 §5B（gateway 控制面库逐表分类）两项阻断性前置。
3. **`5A.3` 未做**：移除 API 响应体的 config 真实路径会连带改
   `proto/boxteam/workspace/v2/public.proto` 的 `ConfigSourceDTO.path` 与 4 个受控生成目录、
   `tests/contracts/protocol_baseline.json` 与两个 protocol 契约测试，属独立破坏性协议切片。
   当前 `app/api/config.py` 的 `path` 字段名保留、值改为 `source.vrn or ""`，标注 `TODO(5A.3)`。
4. **gateway scope_id 仍为字面量**：`X-BoxTeam-Gateway-Id` 请求级注入未实现，
   `_layer_scope_identity` 留有 TODO；15 个 router 的请求上下文注入属另一切片。

## 建议恢复顺序

1. `git status --short` + `git diff HEAD` 确认工作树与受保护改动。
2. 先做 `migrate-identifiers-to-uuidv7` 的 §5A/§5B 两个阻断性前置审计，再动迁移实现。
3. 处置 `user` scope 的规范与代码落差（归 `add-unified-virtual-resource-addressing` 的 owner）。
4. 再做 `5A.3` 协议破坏性切片（单独一笔，含 4 个生成目录同步）。
5. 每个切片保持「一个改动 + 一个独立审查」的分离，审查方必须是作者以外的人。
