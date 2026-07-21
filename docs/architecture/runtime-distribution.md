# BoxTeam 运行时与分发架构

## 发行来源与运行模式

BoxTeam 区分“代码从哪里来”和“以什么方式运行”，不为每种安装方式维护独立的服务编排。

| 发行来源 | 运行模式 | 默认数据目录 | Web UI | Python |
|---|---|---|---|---|
| 源码 checkout | development | `~/.boxteams-dev/` | Vite/HMR | 仓库 `.venv` |
| 源码安装产物 | installed | `~/.boxteams/` | Gateway 静态资源 | 安装产物内置 |
| npm 安装产物 | installed | `~/.boxteams/` | Gateway 静态资源 | 平台包内置 |

`BOXTEAM_HOME` 可以显式覆盖默认数据目录。源码安装和 npm 安装必须使用相同的 staging 布局、runtime manifest 和启动逻辑；二者只允许在产物来源和升级方式上不同。

## 进程所有权

```text
BoxTeam Launcher
└── Gateway
    ├── 默认工作区运行时
    │   ├── Workspace API
    │   ├── Terminal Manager
    │   └── Browser Manager
    └── 其他本地托管工作区运行时
        ├── Workspace API
        ├── Terminal Manager
        └── Browser Manager
```

Launcher 只监督 Gateway，负责前台进程、实例锁、信号转发和发行资源定位。Gateway 是所有本地托管工作区的生命周期所有者，包括默认工作区。Workspace API 负责 Job、工具调用和业务状态的排空及中断对账。

用户显式提供的外部本地后端不归 Gateway 所有，只能健康探测。Gateway 不得尝试终止无法证明所有权的进程。

## 远程 Gateway

远程连接的控制边界是 Gateway，不是单个 Workspace API：

```text
本地 Gateway
└── SSH loopback tunnel
    └── 远程 Gateway
        ├── 远程工作区 A
        └── 远程工作区 B
```

本地 Gateway 从远程 Gateway 发现其直接管理的工作区，并把业务请求、SSE、辅助服务请求和后端重启请求委托给远程 Gateway。首个协议版本不导出远程 Gateway 从第三个 Gateway 导入的工作区，避免递归、循环和不清晰的进程所有权。

Gateway-to-Gateway 凭据与浏览器本地凭据分离，只存放在 `${BOXTEAM_HOME}/state/gateway/` 控制面目录，不进入工作区 `.boxteam/`、前端 DTO 或日志。

## 数据目录

```text
${BOXTEAM_HOME}/
├── config/
│   ├── boxteam.jsonc
│   └── config.schema.jsonc
├── state/
│   └── gateway/
└── installations/
```

工作区业务数据继续位于 `${workspace}/.boxteam/`。Launcher、Gateway 和安装器不得把会话、检查点或工具结果写入全局控制面目录。

同一个 `BOXTEAM_HOME` 同一时间只允许一个 Launcher 监督的 Gateway。实例锁必须记录发行版本和进程信息；发现锁冲突时应报告现有实例，不能清理未知进程。

## 配置生命周期

配置生成属于安装或显式初始化动作：

- `boxteam config init` 只创建缺失配置。
- `boxteam config init --force` 才允许整文件重建。
- 普通 `boxteam start` 只验证和加载，不覆盖用户配置。
- development profile 通过显式 overlay 提供测试工具，不污染 installed 配置。
- 工作区配置覆盖用户配置；只有最终有效配置发生变化才触发 reload revision。

## Runtime manifest

每个可运行产物携带版本化 manifest，至少声明：

```json
{
  "schema_version": 1,
  "distribution": "npm",
  "version": "0.1.0",
  "python_executable": "python/bin/python",
  "application_root": "application",
  "web_assets": "web/dist",
  "chromium_executable": "chromium/chrome",
  "node": {
    "source": "launcher",
    "executable": null
  }
}
```

所有相对路径以 manifest 所在运行时根目录解析。Launcher 不通过当前目录、`.git`、`pyproject.toml` 或自身父目录猜测安装形态。

npm 发行版的 Node 来源为 `launcher`，即使用执行 `boxteam` 的 `process.execPath`。

> TODO: Windows `.exe` 和 Linux 独立安装程序必须携带 Node.js，并在 manifest 中使用 `node.source=bundled` 与明确的相对可执行路径。实现独立安装程序时不得增加 PATH 回退。

## 正式 Web UI

development 模式由 Vite 8011 提供 HMR，并把 `/api` 转发到 Gateway 8014。installed 模式不启动 Vite；Gateway 在同一 origin 提供构建后的 Web UI。

静态资源和 SPA fallback 必须位于 API、SSE、WebSocket 与辅助代理路由之后。未知 `/api/*` 必须返回 API 错误，不能返回 `index.html`。

## 后端安全重启

安全重启流程为：

1. Gateway 获取目标工作区重启锁。
2. Workspace API 进入 `draining` 并拒绝新 Job。
3. Gateway 等待活动 Job、工具和持久化写入，默认最多 30 秒。
4. 超时且未显式 force 时，保留旧后端并返回 blockers。
5. force 时先把受影响任务持久化为 `interrupted`。
6. 停止旧 Workspace API，保留 Terminal 和 Browser。
7. 启动新 Workspace API并通过健康和 workspace identity 校验。
8. 新进程启动时对账任何失去执行所有者的旧 `running` 状态。

不得让两个完整 Workspace API 同时操作同一工作区数据。未来若引入 standby 启动，候选进程在 promotion 前不得接受任务或启动后台调度。

## Linux npm 分发

首发平台为 Linux x86_64 GNU：

```text
boxteam
└── optional dependency: @boxteam/runtime-linux-x64
```

平台包包含可重定位 Python 3.12、锁定 Python 依赖、BoxTeam 应用、生产 Node 依赖、Playwright Chromium、Web dist 和 manifest。构建过程不能复制仓库 `.venv`，且必须在与构建目录不同的路径完成 relocation smoke test。

默认打包命令只生成和验证 npm tarball，不执行 `npm publish`。公开发布必须是独立、显式的 CI 操作。
