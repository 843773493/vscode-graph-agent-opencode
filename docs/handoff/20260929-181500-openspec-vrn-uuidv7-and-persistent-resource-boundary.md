# OpenSpec / VRN / UUIDv7 与持久资源边界交接

> 2026-10-03 调度整理：本文保留历史记录，旧模型、端口禁令与 Git 命令不再作为当前执行指令。尤其本文早期的 commit pathspec 与共享索引 reset 示例已被后续事故推翻，禁止照搬。继续任务以用户当前决定及 [团队协作技能](../../.codex/skills/team-collaboration-gpt/memory/2026/10/04/team-collaboration-workflow.md) 为准。

- 交接时间：2026-09-29 18:15:00 UTC+8
- 仓库：`/data/hyf/20260629_agent/vscode-graph-agent-opencode`
- 分支：`main`
- 当前 HEAD：`ac76554f 文档(openspec): 补登 add-workspace-persistent-resource-management browser 越限项`
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

### 独立审查与屎山审计驱动出的修复

- `24666ff5` `/api/v1/config` 的 `metadata.config_path`/`source_paths` 实测仍对外吐真实路径
  （与 5A.x 收敛方向相反且无测试守护）——**物理删除**这两个键，未用空串/省略号替身；
  新增真实路径形态的负向断言。同笔删除恒返回 `None` 的空壳 `_config_layer_vrn`。
- `fec68743` `legacy_adapter.py` 的 `_non_empty_string` 经 `diff` 确认与
  `validation.py` 的**逐字相同**（上一轮折叠的「显式排除」判断有误），折叠为单一定义。
- `149ce9dc` 修正 5 处与现状不符的 AGENTS.md 登记。
- `f0888be3` + `7fe347dc` 登记全仓未登记越限项（判据式 + 带提交 hash 的实测快照，
  含二级子目录；33 个生产 >800 行文件逐一核对落位）。
- `6ec6a6d7` **根除 `workspace_config` 表级双轨**：专项取证判定它是权威表
  `config_source_layers` 的真子集镜像（列集真子集、内容可完全推导、写点在同事务重复 upsert、
  `config_service.py` 的 legacy 兜底经探针证实生产可达），故整表物理删除 +
  一次性显式迁移；`gateway_config` **不删表**——它还承载控制面独有的
  `workspace_registry_meta` 与 `gateway_connection_ids`，只去掉其 config 来源层镜像角色。
- `2ab3f980` 锁定「仅有 wc 行、无 layer 行」的旧库还原（保证 `get_runtime_override_keys()`
  不丢 `('ui',)`），变异删掉补行逻辑即变红。
- `ac76554f` 补登 `src/workspace-services/browser/` 的越限项（唯一因并发占用遗留的一批）。

## 最新验证结果（本会话实测）

- `pytest tests/unit -q -p no:randomly`（带 `ulimit -d 4194304`）：
  **4913 passed, 7 skipped, 0 failed**。修复前为 79 failed / 4830 passed。
- `/home/hyf/.bun/bin/openspec validate --strict --all`：**40 passed, 0 failed**。
- `import app.main, app.gateway.main`：EXIT=0。
- 工作树对 HEAD 干净（`git diff HEAD` 为空）。
- 独立审查（`round_final_review`）结论：VRN/config 迁移、持久资源三项修正、领域子包归位与
  helper 折叠**均站得住，不需要修正**；`vrn=None → path=""` 经全仓核验**无任何消费点读取**，
  不构成现实的虚假默认值（建议 5A.3 落地时改为可选字段或更名）。
- 独立审查实测：截图端点三分支（id/browser/文件缺失）响应体与响应头均不含绝对路径；
  四处负向断言（保留旧路径列、`path` 回真实路径、快照回 `cwd`、工具结果回 `cwd`）变异全部变红。

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

1. ~~VRN `user` scope 未落地~~ **已由 `f6fc990f` 消除**（见上节「本轮增量」）。
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
3. （已消除，见上）原 `user` scope 落差。
4. 再做 `5A.3` 协议破坏性切片（单独一笔，含 4 个生成目录同步）。
5. 每个切片保持「一个改动 + 一个独立审查」的分离，审查方必须是作者以外的人。

## 本轮增量（HEAD 推进到 `cbf81075`）

- `f6fc990f` **落地 VRN `user` scope**：`_SCOPE_KEYWORDS` 扩为
  `{workspace, user, gateway, inline}`，新增 `app/core/user_identity.py::user_scope_id()`
  恒为 `local`，resolver 的 scope 绑定校验贯通。上节「VRN `user` scope 未落地」一项已消除。
- `8283b076` `user`/`user_local` 的 config 来源仍取 `vrn=None`，但依据改写为真正的
  不可寻址事实（共享 `workspace.sqlite` 载体），删掉了已失效的「scope 闭集不含 `user`」借口。
- `96619538` + `69af9b8b` 台账真实性审计后的补勾与精确化：`migrate-identifiers-to-uuidv7`
  8.1–8.4/9.1–9.2 补勾；`add-itemized-rollout-context` 8.3 由「笼统未实施」精确化为
  **持久化层未 thread 化**的具名落差描述。**§5A（哈希/幂等键审计）与 §5B（控制面库逐表分类）
  仍未勾**，为迁移的前置门控。
- `10f5ae3d` 收口 uuidv7 规划审查 A1–A7 六项必办（虚假证据限定生产代码、窗口期改为原子消除、
  价值主张按实测收紧、迁移面改为 33 前缀 × 持久面矩阵、补 `default_idempotency_key` 漂移、
  补四处空洞义务载体、收口第 15 处 v4 残留）。
- `cbf81075` **thread-qualified 定位走路线 B**：`storage/service.py` 的
  `root()`/`index_path()`/`jsonl_path()` 参数由 `thread_id` 正名为 `session_id`，命中
  canonical `thr_*` thread id 或裸 `main` 别名时显式 `RuntimeError` fail-closed；
  `resolve_session_node_for_runtime` 保持 session-only，无兼容分支、无双读。
  8.3 **主体义务仍未落地**（thread-qualified rollout/index、`database_meta` 单行隔离与
  存量迁移），量化依据见 `out/tests/temp/thread_qualified_persistence/artifacts/report.md` §2。

### 本轮审查推翻的用户主张（勿再转述）

- 「`replaceable_source` 仍暂存在 metadata 且有 TODO」**不成立**：`request_plan.py:65`
  早已是 typed core 字段，metadata 路径已由 `2a3e2863` 物理下线。
- 「`ResourceActivation` 三个 Ref 符号零命中」**不成立**：三个符号均有定义与生产调用点；
  真缺口是 `ResourceActivationCoordinator` 生产零调用、seal 链路未接 activation（归 8.3-A）。
- SessionThread 统一 owner 主张**成立但需修正定性**：领域层（`ContextRef`/`TurnRecord`）
  **已 thread 化**，未收敛的是持久化层（`database_meta` 单行 singleton、`turns.turn_ordinal`
  全局唯一、canonical 表普遍无 `thread_id`）。危害是 child 与 main 会写同一个
  `rollout/index.sqlite` 并串扰 ordinal/active view；代码在 `queries.py:207-215` 自述承认。

### 在途切片

- `selection_role`/`replacement_policy` typed core（规范已定稿于
  `itemized-rollout-context/spec.md:856`/`design.md:620`，代码缺失）：领域实现已完成并验证
  （`tests/unit/domain/itemized/` 1221 passed），卡点是 `schema_upgrade.py:133-146` 要求
  v1 列集合等于当前 owner 列集合。owner 已裁定放开 `storage/schema_upgrade.py` + v1 fixture +
  `schema_version` 步进，由原切片完成后补独立审查。补丁备份于
  `out/tests/temp/selection_role_typed/artifacts/selection_role_typed.patch`。

### 环境教训（本轮卡死根因）

## 第二轮增量（HEAD 推进到 `51250b24`）

### 已交付并独立审查

- `1ac461eb` + `f5a207ed` **`ContextContribution` typed 选择/替换策略落地**：
  `selection_role=direct|backing_only`、`replacement_policy=immutable|replaceable`，闭集必填、
  无第二别名，贯通构造/序列化/SQLite 读写/恢复/composer 过滤；两处 inert `selection_only`
  物理下线；`schema_upgrade.py` 的守卫收紧为「既有列是目标列的具名子集且差集恰等于新增列集合」，
  未知列/缺非扩展列/新增列缺可回填默认值三路 fail closed。两字段**不进任何哈希**，迁移不重算。
  **独立审查（作者≠审查者）结论：接受**——自跑 3 条变异全红，哈希 preimage 清单与实测一致，
  断言无削弱，台账不勾选 1.5/1.6 诚实。
- `2c47e077` **集合型 mutation 横切乐观合同并入 `add-itemized-rollout-context`**（新增 §10）：
  操作性质决定收敛策略的判据、集合成员变化禁止成功路径全量重取、文件树与 Gateway 工作区导航
  接入同一 202 意图协议、目录列表读取工作量必须有界、先量后改硬门控。用户裁定三项：并入
  既有 change（不新开）、本轮只落规范（后改为做 P0+P1）、文件树也接同一 202 协议。
- `33da3c48` **消除会话列表读放大**：`list()` 此前先取全部节点、再对**每个**会话读 `session.json`
  （`limit` 只截断返回条数）。改后排序与 `total` 走索引、manifest **只读当前页**。实测（200 会话
  隔离工作区）改前 `limit=5/20/50` 恒读 200 次，改后 = 页大小。**独立审查：有条件接受**。
- `a4158a59` 收敛 `serde/registry.py` 与 `validation.py` 逐字重复的非空字符串校验（行为不变，变异验证齐）。

### uuidv7 迁移已解锁

- `5d6e1460` + `4390784b` + `08f9caae` **§5B 与 §5A 两条阻断性前置全部收口**：
  §5B 控制面逐表分类落定（只有两个库；`user_view_state` 与 `federation_route_hint` 归 `migrate`，
  归 `explicitly_invalidated` 的为 0 张 → 5B.3 空集满足）；§5A 补齐 **191 个命中文件全覆盖**、
  F 类 68 文件逐符号处置、未落定 0 条。**§6 迁移实施已解锁**。

### 诚实停线与被推翻的结论

- `d8d8f926` **activation 冻结点切片推迟**（未产生任何代码改动）。三条实测阻断：生产
  `ResourceRegistry` 不是可复用单例（`agent_factory.py` 每次 invocation 新建）；冻结在当前生产
  Registry 上**必然 fail-closed**（`unknown-resource-kind` / `resource-registry-empty`）；
  protected body store 未装配（`container.py` 构造 `RolloutCheckpointRuntime` 未传 `protected_detail_key`，
  全仓无生产密钥来源）。且「MCP target 级子 binding」形态在代码里不存在，属需 owner 新定稿的数据结构。
  裁定：与 §8.3-A 一笔纵向切片同时闭合。
- `51250b24` 更正 §10.2 的一处事实错误：`title_source` **既不在 manifest 也不在索引**，
  `get()` 返回的 `default` 是 `SessionDTO` 字段默认值。连带发现两个既有缺陷：`create/update` 传入的
  `title_source` 不持久化；`node.updated_at` 恒等于 `created_at`。

### 本轮最终验收

- `pytest tests/unit -q -p no:randomly`（带 `ulimit -d 4194304` 进程外保护）：
  **4938 passed, 7 skipped, 0 failed，EXIT=0**（20:36）。
- `openspec validate --strict --all`：**40 passed, 0 failed**。
- 机器卫生：清理了 5 天前的僵尸探针进程（`probe_bound.py`、`audit_refs.py`），load 由 36 降到 13。

- 三条链路曾同时卡在**并发跑全量 `pytest tests/unit`**（单次约 20 分钟，机器 load 峰值 38）。
  修复办法：subagent 一律禁跑全量，只跑聚焦目录并用
  `timeout 600 bash -c 'ulimit -d 4194304; exec "$@"' bash uv run pytest -q -p no:randomly <路径>`
  包住；全量由 owner 最后统一跑一次。
- 机器上曾残留 5 天前的僵尸探针进程（`/tmp/openspec_domain_sink/probe_bound.py`、
  `/tmp/bug_hunt_agents_tools/audit_refs.py`），已清理。

## 第三轮增量（2026-09-30，HEAD `10d07f29`）

### 本段新增提交（自 `33da3c48` 起，新→旧）

- `10d07f29` 清理(死代码): 删除 `app/agents/tools/skill_loading.py` 的零引用 `_RESULT_KEYS` 常量
  （1 文件 −15，0 新增）。A 段 5 条现已全部做完。
- `f04fb787` 整理: 修 `64ba30c8` 触及 4 文件的 I001 导入排序，并把 3.4-A owner 裁定与
  3.4-C 未注入通道补登进 `tasks.md`（5 文件）。
- `15c2b8d0` 文档(openspec): 勾选 3.4-A 并补登实测，登记 3.4-B 重启恢复缺口。
- `64ba30c8` 实现(gateway scope): 请求级注入 `X-BoxTeam-Gateway-Id` 贯通 skill catalog（3.4-A，15 文件）。
- `ccec61a0` 测试(skill_load): 锁定 rebind 与 already_active 两维度组合边界并校正台账（6.2-B 收口）。
- `357728b7` 去重: `fork/validation.py` 复用 `assembly/validation.py` 的 5 个 manifest 校验 helper（−23）。
- `2ed1d2e1` 清理(死代码): 删除 `turn_history` v1 模型与 8 处零引用符号，折叠两 worker 重复校验（+7/−197）。
- `a49bb0b0` 实现(skill_load): tracked 显式 rebind 与闭集取值 `rebound`（6.2-B）。
- `f670d36c` 规范: migrate 拒绝码口径与 owner 侧三套登记一致（只引用 grammar/resolve 两套）。
- `d649d418` 修复(会话目录): `_snapshot` 收敛为单次全 catalog 聚合。
- `8dbbbce4` / `5c36fad2` 规范: VRN 8 项规范收口（引用侧具名化 / owner 侧闭集与行号）。
- `16ff71af` 规范: 更正 §10.3a 的 `resolver.revision` 记账错误，登记 S8 与 §10.3b。
- `532e25c6` 修复: §10.3a 消除会话目录快照 O(N^2)（`get` 改单节点查询与父链上溯）。

### 已通过独立审查（作者≠审查者）

| 提交 | 审查结论 | 报告 |
| --- | --- | --- |
| `d649d418`（§10.3b 方案 A） | 接受 | `out/tests/temp/review_10_3b/artifacts/REVIEW.md` |
| `5c36fad2`+`8dbbbce4`（VRN 8 项） | 接受 | `out/tests/temp/review_vrn_spec/artifacts/REVIEW.md` |
| `2ed1d2e1`（死代码清扫） | 接受 | `out/tests/temp/review_redundancy_cleanup/artifacts/REVIEW.md` |
| `a49bb0b0`+`ccec61a0`（6.2-B rebind） | 有条件接受→两条件已在 `ccec61a0` 收口 | `out/tests/temp/review_skill_rebind/artifacts/REVIEW.md` |
| `357728b7`（B1 去重） | 接受 | `out/tests/temp/review_dedup_b1/artifacts/REVIEW.md` |
| `64ba30c8`+`15c2b8d0`+`f04fb787`（3.4-A） | 有条件接受→四条件由作者在 `f04fb787` 收口，owner 已复验 | `out/tests/temp/review_gateway_scope/artifacts/REVIEW.md` |

### owner 裁定（本轮新增）

- **3.4-A fail-closed 触发条件收窄获书面确认**：触发条件为「gateway 层有条目」；无条目时不
  物化任何 gateway-scope URI、不产生虚假身份，故不强制 fail-closed。已登记进 `tasks.md` 3.4-A 子项。
- **B1 权威定 `assembly/validation.py`**：依赖方向单向 `fork → assembly`；统一后消息**必须仍含
  `manifest` 字样**（否则打穿 `test_turn_execution_recovery.py:1988` 的 `match="assembly manifest|manifest"`）。
- **B2 不合并**：Gateway 控制面库与工作区库是两个独立持久化 owner，`config_pending_candidate` 的
  `last_attempt_id/last_apply_id` 在两库迁移序列已实际分叉，各有独有表/方法。**否决**侦察报告的「−200 行」。
- **B3 不抽**：asyncio stop 模板属合理分层代价。
- **工作树无在途改动（重要）**：本会话末用全新索引
  `GIT_INDEX_FILE=/tmp/root_fresh_probe.idx git read-tree HEAD` 复验，`git diff HEAD` 为 **0 文件**。
  此前 `git status` 的 ~42 个 `MM` 与那条 `D tests/unit/services/mapping/itemized/test_selection_role_projection.py`
  （blob `d94ce0bc` 与 HEAD 逐字相同）**均为共享 `.git/index` 停在 09:45 造成的陈旧假象**，无丢失的工作。
  教训：判工作树一律 `git diff HEAD`；要彻底排除索引假象就用独立索引 `read-tree HEAD` 后比对。

### 复用的 subagent（均在显式 `model=newapi-local/deepseek-v4.1-flash` + 显式 effort 下）

实施类：`impl_10_3b_b`、`impl_skill_rebind_b`、`impl_gateway_scope`、`redundancy_cleanup`、`dedup_b1`、`vrn_spec_fix2`。
侦察/裁定类：`redundancy_scout_v3`、`arch_owner_dedup`。
审查类：`review_10_3b`、`review_vrn_spec`、`review_skill_rebind`、`review_redundancy_cleanup`、`review_dedup_b1`、`review_gateway_scope`。

### 未实现缺口（如实登记，不得写成已修）

- **3.4-B**：重启恢复的 pending job 无 request-scoped `gateway_id`（`PendingRequestDTO` 无该字段），
  重启后队首 job 在 gateway 层 fail-closed。TODO 在 `app/services/business/job/service.py:1422`。
- **3.4-C**：三条代理通道未注入该头——`app/gateway/auxiliary_proxy.py:310`（WebSocket 中继）、
  `app/gateway/runtime/controller.py:539`（lifecycle/config 控制面）、`app/gateway/federation/workspace_port.py:64`（联邦冷目录 port）。
- **6.2-B 后续**：同名层覆盖的同名层生产者侧端到端链路未接（已登记 `add-context-injection-lifecycle/tasks.md:95`）。
- 更早遗留：uuidv7 §6 迁移未开工；§8.3 thread-qualified 主体；8.3-A/9.3 真实 turn/model_call 边界 +
  activation 冻结点（三条实测阻断）；5A.3 移除 API 响应体 config 真实路径；§10.3 剩余 manifest O(N) 读。

### 本轮验收

- `openspec validate --strict --all` = **40 passed / 0 failed**（`/home/hyf/.bun/bin/openspec`）。
- `python -c 'import app.main; import app.gateway.main'` 退出 0。
- `f04fb787` / `10d07f29` 祖先链已核（`merge-base --is-ancestor` 均 0）。

## 第四轮增量（2026-09-30，HEAD `c9eae769`）：config layer 读路径缺陷

### 缺陷（只读取证发现，非命名洁癖）

`layer` 在 config 来源读取上**同一 source_key 两条读路径给出矛盾值**，且**可寻址层被错标**：

- `_config_source` 把 `user`/`user_local`/`workspace` 有损改写成 `sqlite` 后对外暴露，而
  `_persisted_source_details`/`get_source_diagnostics` 报回逻辑层 —— 同一 source_key 报两套 `layer`。
- 更严重：重启后 `inline`（唯一**有 VRN 可寻址**的层）被落进兜底 `("sqlite", …)`，`precedence` 由 0 变 1，
  直接违反既有规范 D9「`sqlite` 层 MUST NOT 编 VRN」。
- Gateway 侧同构残留：`_gateway_source_detail` 硬编码 `layer="sqlite"`，而无 store 路径报 `user`/`user_local`。

### 修复提交

- `523e3cd8` 修复(config): 统一 workspace config 来源 layer 读路径并消除 inline 误标 sqlite。
  引入唯一权威表 `_SOURCE_LAYER_AUTHORITY`（`config_service.py` 约 :95-107），`_config_source`/
  `_runtime_override_source`/`_persisted_source_details`→`_resolve_persisted_layer`/`_source_baseline` 全部查同一张表。
- `91f81079` 台账补登（5.7 勾选 + spec 新 Scenario + 5.8 D-C 登记）。
- `c9eae769` 修复(gateway config): 统一 gateway 可变 override 层的 layer 读路径。
  引入 Gateway 本文件自有权威表 `_GATEWAY_SOURCE_LAYER_AUTHORITY`（`app/gateway/config.py:63`），
  **未** import workspace 的表（遵守 B2「两个独立持久化 owner 不合并」），但取值语义一致。

### 独立复核（作者≠审查者）

| 提交 | 审查结论 | 报告 |
| --- | --- | --- |
| `523e3cd8`+`91f81079` | 有条件接受（workspace 侧忠实；点名 Gateway 侧残留） | `out/tests/temp/review_config_layer/artifacts/REVIEW.md` |
| `c9eae769` | 接受（无虚报，缺陷消除、单一权威、变异红、台账诚实） | 同上「Gateway 侧收口复核」节 |

### 登记未实施（D-C，不在本切片）

layer **值集**的破坏性两轴分离：`sqlite` 一名承载 runtime override 与物化快照两义、
`active_snapshot`/`pending_snapshot` 用 `layer="sqlite"`/`precedence=1` 与可编辑层 precedence 重叠、
`ConfigUpdateRequest.config_layer` 读写异名。涉及 proto + `buf generate` + `bun run gen:openapi` + 前端类型，
已登记为 change 1 的 5.8「已裁定、待实施」。

### 本轮验收

- 聚焦：workspace 侧改前 140 / 改后 141 passed；gateway 侧改前 97 / 改后 98 passed，失败集合两版均空。
- `openspec validate --strict --all` = 40 passed / 0 failed；`import app.main`/`app.gateway.main` 退出 0。
- `10d07f29`、`523e3cd8`、`91f81079`、`c9eae769` 祖先链均已核。

## 第五轮增量（2026-09-30）：并发协作约定与台账集中裁决

本节由台账唯一 writer 追加（append-only，既有内容一字未改）。以下四条是本轮 owner 已定的并发协作约定，后续 agent 一律适用。

### 约定 1：openspec 台账与 handoff 文档写权集中到单一 writer

openspec 台账（`openspec/changes/**/tasks.md`、spec、design）与本 `docs/handoff/` 文档的**写权集中到单一 writer agent**；其他 agent 对这些面**只读**，需要落账时**向 writer 报告**，由 writer 转交落盘。设计意图：把「多 agent 并发写 tasks.md」的冲突集中到一个 writer 上，避免并发覆盖。

### 约定 2：共享 `.git/index` 已损坏不可信

共享 `.git/index` 已损坏、不可信（曾出现 5 条 `D` 假象 + 大量 `MM` 陈旧条目）。据此：

- 判改动一律用 `git diff HEAD`，不用 `git status` / 暂存区推断。
- 提交一律用独立临时索引，正确顺序：`GIT_INDEX_FILE=/tmp/<任务>.idx git read-tree HEAD` → `GIT_INDEX_FILE=/tmp/<任务>.idx git add <精确路径>` → `GIT_INDEX_FILE=/tmp/<任务>.idx git diff --cached --name-only` 逐条核对只含自己的文件 → `GIT_INDEX_FILE=/tmp/<任务>.idx git commit -m "中文"`（**不带 pathspec**）→ `git show --name-status` + 两条 `git merge-base --is-ancestor`。**commit MUST NOT 携带 `-- <精确路径>` pathspec**（详见下方「约定 5」）。
- 5 条 `D` 假象路径**绝不 add / patch / 删**：`app/abstractions/turn_terminal_status.py`、`tests/unit/core/test_identifier_uuidv7_monotonic.py`、`tests/unit/gateway/server/test_workspace_proxy_route_reference.py`、`tests/unit/gateway/test_gateway_identity_proxy_channels.py`、`tests/unit/services/mapping/itemized/test_selection_role_projection.py`。
- 严禁 `git commit --amend`（多 agent 共用工作树）与直接操作共享 `.git/index`（`git add` / `reset` / `rm --cached` / `update-index`）。

### 约定 3：受保护路径三对象全程禁碰

以下三对象在任何切片中**全程禁碰**（只读，不得修改、移动、删除）：

- `app/gateway/control/generators.py`
- `app/services/business/session_generation/service.py`
- `examples/demos/Itemized_context_storage/`

### 约定 4：测试必须带进程外保护

跑任何测试都必须带**进程外保护**，禁裸 `pytest` / `bun test`：`timeout <秒> bash -c 'ulimit -d 4194304; exec "$@"' bash <命令>`。历史事故：工作树测试与生产代码错配时，`useSessionMessageStream.test.tsx` 的 410 重连用例进入永不收敛紧循环，内存无界增长（t=66s 已 6.9GB），曾把整机 16GB 吃到 OOM。优先走 `bun run test:matrix -- --suite=<id>`。

### 约定 5：commit MUST NOT 携带 pathspec（会绕过隔离索引吞入他人在途改动）

**这是本会话真实发生过的污染事故，必须严格遵守。** 根因是 AGENTS.md 旧规程里写的 `git commit -m "中文" -- <精确路径>` 本身就是错的（已于提交 `44449fa5` 修正 AGENTS.md，并已在隔离仓库实测确证）：

```text
# 错误：pathspec 会用「工作树当前内容」重建该路径，完全绕过隔离索引
GIT_INDEX_FILE=...idx git commit -m "msg" -- <路径>

# 正确：不带 pathspec 时，commit 才精确提交隔离索引的内容
GIT_INDEX_FILE=...idx git commit -m "msg"
```

- 真实事故：`d4e864fc`（提交去重时）借该形式吞入了他人在途的 `request_id` 改动，由 `de5a0cef` 前向回退。
- 实测确证：`44449fa5`（隔离仓库实测 + AGENTS.md 修正）。
- 正确顺序（务必照做）：`GIT_INDEX_FILE=/tmp/<任务>.idx git read-tree HEAD` → `GIT_INDEX_FILE=/tmp/<任务>.idx git add <精确路径>` → `GIT_INDEX_FILE=/tmp/<任务>.idx git diff --cached --name-only` **逐条核对只含自己的文件**（发现混入立即 `git read-tree HEAD` 重建索引后重新精确 `git add`，禁止 `git rm --cached`）→ `GIT_INDEX_FILE=/tmp/<任务>.idx git commit -m "中文"`（**不带 pathspec**）→ `git show --name-status` + 两条 `git merge-base --is-ancestor`（本提交是当前 HEAD 祖先 + 提交前读取的 HEAD 仍是祖先）。
- 判改动一律 `git diff HEAD`；因带 pathspec 的 commit 会用工作树内容重建路径，任何 commit 前的暂存核对都必须用隔离索引的 `git diff --cached --name-only`，不得依赖共享 `.git/index`。
