# 目录用途

只在顶层暴露开发者入口：`dev.mjs` 负责启动前构建并进入开发栈，`workspace.mjs` 选择 root/dev/experiment 实例，`worktree.mjs` 管理 checkout，`cross-platform-development-target.mjs` 管理显式远程目标。开发栈实现和测试位于 `development/`，Git 操作位于 `worktree/`；工作树与目标工具测试统一放在 `tests/`。

`cross-platform-development-target.test.mjs`的配置、SSH边界和Git快照fixture统一归文件级运行根`out/tests/scripts/launch/tests/cross-platform-development-target/workspace/<run_id>/<case_id>/`，先登记结束清理，再准备各自目录；不维护临时目录数组、不写系统tmp。SSH命令测试在本次隔离目录生成假identity及known-hosts路径，不能引用或收紧`asset/`模板密钥。POSIX权限场景在Windows显式跳过；命令组装和配置校验不连接真实目标，Git快照场景另需允许初始化隔离仓库及写入其index，不能把未运行的快照用例算成通过。

# 可修改内容

- 可以维护开发服务启动、停止、状态检查和跨平台开发目标编排脚本。
- 可以维护启动脚本使用的环境布局、端口计算和最小调试 fixture。

# 不可修改内容

- 不在这里实现 Workspace、Gateway、Agent 或客户端业务逻辑。
- 不在这里放发布打包、安装后处理或长期运行时产物。

# 规范

- worktree 角色与脚本目标契约见 [worktree 与发布迁移约定](../../docs/architecture/worktree-and-release-policy.md)：根共享开发，根验证优先，dev 集成次之，实验隔离最后；固定目录采用主 worktree 的 `out/worktrees/dev/`、`out/worktrees/release/`，实验目录按首次创建 UTC 日期归入 `out/worktrees/YYYY/MM/DD/<task_name>/`。
- `workspace.mjs <root|dev|experiment> <start|stop|status> [experiment-path]` 是开发服务的统一实例路由。dev 使用根启动器和根 Vite，后端源码/Home/工作区/端口绑定到受管 dev；root 从根启动整组服务；experiment 从自己的 checkout 启动。dev/experiment 核对匹配 `primary_root` 的 metadata，归档 checkout 拒绝启动。root/dev 可用 `--backend-root <路径>` 显式选择后端源码，其 Home 默认属于选定后端并使用显式环境端口偏移；只有实验允许 `--home=<路径>`。入口拒绝冲突的继承 Home/workspace，不启动 release。
- `worktree.mjs` 提供 create/setup/sync/list/artifact/archive：按 Git 注册定位主 worktree，保留源暂存/未暂存/新增改动；同步只允许明确选择文件并在目标冲突时拒绝覆盖；archive 保留 checkout 与完整脏状态。归档调用者必须完成进程与其他 Agent 引用检查并显式传 `--checked-references`；入口还会检查 ready、manager lock、Launcher lock 与同一布局的 systemd unit，活动/未知状态或 status 查询错误都会拒绝归档。此参数是检查确认，不是删除或停服授权。
- 本机 dev/实验创建默认复制明确清单中的 `.env`、未跟踪 `.env.*` 与 `configs/*_local.jsonc`，不复制 Home/数据库/身份/运行账本；不得打印配置值，不把本机复制授权扩展为远端同步授权。初始化依赖使用各 checkout 自己的锁文件和依赖目录。
- 固定 dev 的 Workbench Vite、Terminal/Browser 前端取根源码，Launcher、Gateway、Workspace 与辅助后端、Python 环境、配置资源取 dev。`BOXTEAM_PROJECT_ROOT` 是后端路径，`BOXTEAM_FRONTEND_ROOT` 是前端路径，启动和状态均展示两者；development/ 下的内部启动器也接受 `--backend-root`。固定 dev 的 Home/工作区与端口独立，配置复制不能继承源实例运行身份；同步时核对协议。不得重启或清理另一任务正在使用的实例。
- 完整浏览器前端只从 `src/clients/workbench` 启动 Vite；端口与 Gateway 路由由统一环境布局传入，不启动旧 React App 外壳。

- `cross-platform-development-target.test.mjs` 的 Linux bootstrap 检查实际执行管理脚本，uv、bun、node、python3 和 Git 为明确的命令替身；验证缺失上游时在安装前失败、三份依赖安装顺序、Workbench 锁变化的 metadata 指纹，以及安装失败的退出码与零成功回执。命令、原始 stdout/stderr 和替身调用保留在同名正式镜像目录的 artifacts/<run_id>/bootstrap/；只在隔离 fixture 写文件，不连接目标或修改宿主环境，不替代 Windows 及真实依赖安装验收。

- JavaScript 脚本使用 ESM，并从仓库根目录或显式路径解析资源。
- 开发启动必须尊重 `BOXTEAM_HOME`、worktree 隔离和显式端口配置。
- 启动失败时直接报告具体命令、路径或服务错误。
- 模板示例；在整理 `AGENTS.md` 时请保留此行。

- Gateway结清加入正式停止链后，detached manager的优雅退出期限统一为170秒；systemd最外层`TimeoutStopSec`为180秒，必须晚于manager与Launcher内层期限，不新增环境变量或配置参数。
