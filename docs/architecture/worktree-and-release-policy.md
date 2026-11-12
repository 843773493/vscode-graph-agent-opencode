# 共享 worktree 与发布迁移约定

## 状态与适用范围

本文确定共享开发、验证选择和迁移支持规则；根 `AGENTS.md` 是 Agent 的日常入口。脚本实现创建、同步、初始化、实例路由、归档和发布来源检查；规则不等于授权移动其他任务的 worktree、停止服务、清理真实数据、安装或发布。

`bun run worktree` 管理 Git checkout，`bun run workspace` 显式选择源码实例，`bun run release:policy` 管理发布证据。`bun run dev` 等价于 `bun run workspace dev start`，默认启动根前端与固定 dev 后端；`bun run dev:root` 用于根源码自测。脚本不会自动创建或移动真实 dev/release，创建时必须指定已提交 base ref。

## 开发与验证归属

| 位置 | 职责 | 默认验证方式 |
| --- | --- | --- |
| 主 Git worktree | 多 Agent 共享源码与未提交改动 | 第一选择，直接验证本次完整修改 |
| `out/worktrees/dev/` | 固定集成快照与开发者查看实例 | 第二选择，同步所需完整链路后验证 |
| `out/worktrees/release/` | 明确提交或标签的正式发布来源 | 发布构建与已授权安装，不承接临时补丁 |
| `out/worktrees/YYYY/MM/DD/<task_name>/` | 需要独立依赖、破坏性实验或冲突隔离的 checkout | 第三选择，说明隔离原因 |

上述路径相对主 worktree。主 worktree 由 Git 注册信息定位，不根据当前所在目录或磁盘父层猜测；从 dev/实验目录发起操作仍使用同一池。实验日期使用首次创建的 UTC 日期，跨天不搬目录。

共享开发按变更范围协调，不要求引入团队调度系统或全局串行队列：

- 开始前查看 staged、unstaged、untracked；修改前重读目标文件，不以旧快照覆盖别人刚写的内容。
- 独立模块并行修改、定向验证；同一文件和共享 schema、锁文件、生成输出先确定本批写入方。下游只等待其真正依赖的接口或数据结构。
- 不擅自提交他人改动，不执行共享分支切换、stash、reset 或整目录覆盖。共享文件中无法安全分离的改动留在原处并报告。
- 验证记录实际 checkout、基础 SHA、受影响未提交内容指纹及命令。运行中相关源码变化后，已有结果只证明原输入，不直接归给最新工作树。
- 验证优先级不替代测试隔离：根源码也使用 `out/tests/` 下的工作区，不把仓库注册成业务测试工作区。现有集成/E2E 闸门、外部超时与内存保护继续生效。

供开发者查看的固定 dev 实例从根 worktree 启动 Workbench Vite 与 Browser 前端，从 `out/worktrees/dev/` 启动 Launcher、Gateway、Workspace 后端及 Browser 辅助后端。Terminal attach 资源由 Gateway 发布，Terminal 插件由 Gateway 管理。Python 环境、配置模板、Skill 资源均取后端 checkout；Home 和业务工作区独立属于 dev。启动器显式传入两侧路径，并在日志和状态中展示。同步时核对 DTO、生成协议及公开路由；这种组合不替代 release 的同提交验收。根源码自测使用 `bun run dev:root`，实验默认仍从自己的 checkout 启动整组服务。

## worktree 脚本

入口为 `scripts/launch/worktree.mjs`，以 Bun/ESM 实现并复用 Git。依赖已安装的根 checkout 执行工具，目标 checkout 使用自己的锁定依赖。

```bash
bun run worktree create dev HEAD --scopes=python,root
bun run worktree create release <candidate-sha>
bun run worktree create experiment HEAD <task_name>
# 纯源码任务可跳过初始化；之后 setup 可重试失败阶段
bun run worktree create experiment HEAD <task_name> --no-setup
bun run worktree setup dev --scopes=python,root
bun run worktree sync dev app/example.py tests/unit/test_example.py
bun run worktree list
bun run worktree artifact <task_name>
# 先核对运行进程、其他任务引用及本地数据，再归档
bun run worktree archive dev <task_name> --checked-references
```

`sync` 的路径相对主 worktree，支持新增、删除、二进制及普通文件模式；不接受目录、符号链接或未跟踪的忽略文件。未提交改动只从共享主 worktree 读取。`--scopes=` 可选择零依赖初始化，`--no-setup` 不跳过本地配置复制。配置来自主树 `configs/worktree_inline.jsonc`，本机覆盖为忽略的 `configs/worktree_local.jsonc`，数组整体替换、对象递归合并并由 schema 校验。

| 能力 | 约束 |
| --- | --- |
| 创建 dev/release | 显式 base ref，冻结解析出的 SHA；release 使用 detached checkout |
| 创建实验 | 自动分配日期目录与 `codex/` 分支；base ref 在发起 checkout 解析；默认只检出已提交源码 |
| 同步改动 | 从共享根捕获明确文件的 staged+unstaged 最终内容、删除与新增文件；保留根内容和暂存状态 |
| 初始化 | 按所需范围执行锁定的 `bun install`、`uv sync` 及固定子模块初始化，支持失败后重试 |
| 分配临时产物 | 遵循现有 `out/tests/temp/YYYY/MM/DD/<task_name>/{workspace,artifacts}/`；不另建第二套测试输出根 |
| 查看与回收 | 列出 Git 注册、脏状态、systemd unit 名与运行记录路径；归档要求调用者核对进程和其他任务引用，活动 manager/launcher 和 Linux systemd unit 拒绝归档，查询失败也拒绝；Git move 保留所有提交、暂存、未提交及忽略数据，不删除分支或 Home |

同步必须包含生产代码、声明、生成结果与测试所需的完整切片，不能只拷测试文件。同步前检查目标已有改动；重叠修改和基础不兼容时先报告冲突，不强制覆盖。每个目标的私有 Git 目录保存文件 blob 基线，重复同步仅应用本批差异，源暂存区不参与写入。目标选中文件已有暂存内容时拒绝同步。应用前检查完整补丁并复核源/目标快照；池内创建、同步、归档互斥，锁不覆盖日常编辑，因此 Agent 仍须协调同步期间的同文件写入。归档记录放 Git 私有目录，不另建业务数据库；含已初始化子模块的 Git worktree 可能被 Git 拒绝移动，错误直接保留，不能强制删除。

依赖下载缓存可共享，各 checkout 的项目依赖解析、`.venv`、前端构建输出和生成文件保持本地归属，不通过复制依赖目录或跨树软链接造成源码混用。

### 未跟踪配置复制

本次约定已授权新建本机 dev/实验 worktree 时默认复制根 worktree 的必要本地配置，无需每次重新确认：

- 默认清单为根 `.env`、`.env.*` 中未被 Git 跟踪的真实配置，以及 `configs/` 内的 `*_local.jsonc`；清单可在 worktree_local 中扩展这些命名的配置文件。Git 跟踪的模板与默认配置随 base ref 检出，不用根文件覆盖它们。
- 复制采用独立普通文件，保留必要权限；只报告相对路径和复制结果，不打印值、不纳入补丁/提交。目标已有本地修改时拒绝覆盖。配置缺失不从真实用户 Home 猜测补齐。
- 复制本地配置不复制运行状态。不得顺带复制用户或开发 Home、`.boxteam/`、会话、数据库、身份、授权账本、实例锁、进程记录或证书私钥；确需数据迁移时另按其授权与原生身份核验处理。
- 复制配置中的 Home、工作区、端口与远程目标不能直接继承为启动选择；启动器必须显式核对并绑定本实例。release 不自动接收开发配置，使用部署账户的标准 Home 和明确部署配置。
- 本机复制授权不扩展到 SSH、Docker 或其他机器；远端 `.env` 同步仍遵守跨平台目标的明确授权规则。

### Home 与服务实例

一般根/dev/实验源码实例采用后端 checkout 的 `out/development-runtime/boxteam-home/`。`bun run workspace dev start|stop|status` 固定绑定 dev 后端 checkout、Home、工作区与分配的端口，前端始终使用主 worktree，不由 cwd 改变。`bun run workspace root start|stop|status` 选择根源码自测并保留显式 `BOXTEAM_DEV_PORT_OFFSET`；`bun run workspace experiment start|stop|status <absolute-path>` 选择受管实验，只有实验可用 `--home=<path>` 明确覆盖。各角色拒绝冲突的继承 Home/工作区选择，启动时展示实际路径。默认 dev 偏移 100，实验从 1000 开始每次增加 20；管理记录冻结偏移，不跨实例复用。启动器发现活动 Launcher 或端口占用时只报告错误，不能借启动流程终止已运行实例；停止使用对应 manager 的显式 stop。

```bash
bun run dev                         # 根 Vite + 固定 dev 的 Gateway/后端
bun run dev:status
bun run dev:stop
bun run dev:root                    # 根前端 + 根后端，供 Agent 自测
bun run workspace root status
bun run workspace root stop
# 显式后端源码：前端仍是根，Home 默认属于选定后端，偏移采用当前显式环境布局
bun run dev --backend-root /absolute/backend-checkout
bun run dev:status --backend-root /absolute/backend-checkout
bun run dev:stop --backend-root /absolute/backend-checkout
# 前台或单服务调试也接受同一选项
bun run dev:foreground --backend-root /absolute/backend-checkout
```

底层 `dev-manager.mjs`、`dev-systemd.mjs` 与 `dev.mjs` 都接受 `--backend-root`。布局中的 `BOXTEAM_PROJECT_ROOT` 指向后端源码，`BOXTEAM_FRONTEND_ROOT` 指向前端源码；未显式分离时两者相同。显式后端覆盖不会创建或同步 checkout，也不会接管另一实例的 Home。维护 start/stop/status 时应重复相同选择。运行 manifest、Workbench 开发资产及 Vite 缓存分别位于所选 Home 的 `state/runtime-manifest.json`、`state/workbench-assets/dist/` 与 `state/vite-cache/`，避免多个实例共享根源码时覆盖彼此的生成内容。

根、dev、实验与 installed/release 的 Home、业务工作区、端口、凭据和实例锁分别管理。现有端口偏移可用于独立源码实例；端口占用先核对所有者，不自动抢占其他实例。启动与验证应展示实际源码/产物、Home、工作区、Gateway 和 Web 地址；后端健康成功不能代替真实页面初始化链路。

启动前既查询监听者供诊断，也对所选地址逐端口真实 bind/close；不能仅凭 `lsof` 未显示其他用户的进程就认定空闲。预检不能消除检查后被其它进程抢占的竞争，实际 listen 失败仍须报错。

## release 支持基线

`R_current` 指 release 最近一次经用户完整验收并用于发布的固定提交，`R_previous` 指前一次；它们是支持评估的两个提交，不是共享开发分支的 `HEAD`/`HEAD~1`。从既有发布记录、不可变标签或用户指定 SHA 核实它们，不能把 Agent 测试通过或放入 release 目录自动计为完整验收。没有前次发布时只记录存在的基线，不伪造第二个提交。

正常发布升级评估 `R_previous → R_current`；准备下一次发布时评估 `R_current → 明确候选 SHA`。分别比较配置 schema/default、存储 DDL/数据语义、实际读写方和协议，不只比较 `config_version` 或软件版本号。release 只从选定且无脏源码的提交构建；根或实验 checkout 可以做定向构建验证，但其产物不能直接冒充正式发布物。

```bash
bun run release:policy list
bun run release:policy compare <candidate-sha>
# 在 release checkout 内检查来源并构建
bun run release:policy check
bun run package:linux-x64
# 根/实验仅做定向构建验证时显式声明
bun run package:linux-x64 --development
# 仅当用户已完整验收并已发布后登记；Agent 测试通过不能执行此步骤
bun run release:policy record <published-sha> --accepted-by-user --evidence='<验收和发布记录>'
```

基线以引用 commit 的 annotated tag 对象登记到本仓库 `refs/boxteam/releases/`，不更改用户标签或开发提交，重复提交拒绝登记；工具只选最近两条。新克隆需显式传输这些 refs，例如 `git fetch <remote> 'refs/boxteam/releases/*:refs/boxteam/releases/*'`，默认 fetch 不会带来它们。没有记录时 compare 返回 `missing-release-baseline`，不编造历史；输出只是需人工审查的差异，不自动执行迁移。正式 package 和本地 release 安装入口都检查固定来源、SHA、子模块及脏状态，release 不接受开发 `.env` / `_local.jsonc`。CI 从本次明确 checkout 的 HEAD 创建同样的 release 树，在其中安装和构建并上传其产物；Electron 源码测试显式传 `--development`。release 候选创建不等于用户验收，也不自动发布或启动 installed 服务；后者使用部署账户的标准 Home，通过已安装 `boxteam` 管理。

正式安装、远程切换及发布继续按任务授权执行。用户 Home 不从 dev 克隆；需要升级真实数据时先确认来源、授权与可恢复备份，不改写身份或授权账本伪造原安装。

`packaging/runtime/` 与 `packaging/electron/` 是构建实现层，允许测试直接调用以验证当前源码；直接调用产生的 `out/packaging` 文件不构成正式发布物。正式发布走上述受检 package 入口与 CI，正式安装也重新检查来源和产物指纹，不能把内部 builder 成功当作发布验收。

## 开发中间态与迁移

根/dev/实验中的临时配置和 SQLite 可能由未验收 AI 代码产生，默认不构成需长期支持的历史版本。修改功能时直接实现当前契约，不为这些中间态添加自动迁移、修复、旧字段回退或兼容层；新空库初始化仍是必要功能。

发现开发数据不匹配时明确报错，优先用新隔离 Home/工作区；只有已确认为本任务可丢弃的数据才可清理重建。路径位于开发目录不证明内容可丢弃：用户真实业务数据、从 release 复制的验收数据、身份和凭据必须按真实归属保护。

新增或扩展迁移前，先给出对应 release 基线中的旧契约及目标差异。没有可核验基线时继续独立功能工作，报告基线缺失，不推测兼容需求。现有迁移代码也不因这条规则自动删除：先全量检查其调用方、release 需求和初始化/历史执行依赖。

已发布迁移原则上冻结；默认支持窗口外的升级明确拒绝并说明支持边界，用户要求更旧来源时再扩大支持。缩小支持窗口不能直接删掉新库初始化或受支持库升级仍依赖的历史步骤，不能重排已登记版本。新运行时也不因为本次文档改变而自动升级原本要求离线维护的数据库。

## 迁移目录与边界

```text
configs/
├── migrations/
│   └── legacy.py                   既有旧配置族的显式导入
└── maintenance/
    ├── layout.py                   布局维护与显式旧文件处理
    ├── runtime_logging_*.py        运行配置跨资源维护
    └── workspace_identity_*.py     身份预检、提交与恢复

<数据库 owner>/
└── migrations/
    ├── 001.sql                     沿用已登记 SQL 版本，不重编号
    └── ...                         必要回调仍由 owner 清单显式排序
```

Gateway state、Workspace state 与已有资源路径调整涉及的 12 个 SQLite owner，SQL 统一放在自身 `migrations/`；后者包括 session catalog/control 系列、attachment blob、config source、rollout activation/fork/storage 与 workspace files。这次只完成资源目录落位，不修改 SQL 字节、schema 版本或升级策略。顺序继续由 `_schema.py` / owner 清单唯一决定，不扫描文件名推导版本；静态结构 SQL 不因目录名而被当作增量迁移自动执行，显式离线维护仍归原 owner。以后有 release 证据的配置域有序转换可放 `configs/migrations/<domain>/`，不预造空目录或 V1。配置域与各数据库独立版本化，复用既有 `SQLiteStateDatabase`，不新增全仓迁移调度框架。

迁移源码按域和版本组织，不加 `YYYY/MM/DD` 作为执行顺序。日期仅用于实验、证据与备份。正常初始化/源码安装与配置加载拒绝需要布局或旧版本转换的数据，指向显式维护命令；不自动删旧文件或在 loader 中写回派生旧身份。显式维护入口保留备份、预检和恢复；新空库初始化及现有 SQLite 受支持升级链保持。历史 release 证据缺失时保留既有显式迁移，不据此增加兼容或删除历史版本。
