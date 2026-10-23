# 目录用途

`Ssh_text_attach/` 是一个跨端 txt 文件 attach 演示项目。宿主机前端连接宿主机后端，后端可直接管理本地 txt 文件，也可通过 SSH 隧道连接容器内同款后端并编辑容器侧 txt 文件。

```text
Ssh_text_attach/
├── package.json            宿主机启动、检查和测试命令
├── Dockerfile              Linux SSH 容器演示镜像
├── compose.yaml             容器角色编排
├── config/                  本地和 SSH attach 目标配置
├── docker/                  容器入口；Linux 只用于容器内 sshd
├── scripts/                 角色启动、Docker 访问、输出背压和进程树回收
├── src/
│   ├── client/              浏览器 attach、编辑、刷新和保存界面
│   └── server/
│       ├── server.js        backend/gateway/frontend 角色入口与 HTTP 生命周期
│       ├── config.js        角色、端口、目标配置和运行路径解析
│       ├── targetFileClient.js 本地目标选择与 SSH 隧道地址解析
│       ├── fileStore.js     UTF-8 文件初始化、串行保存和完整快照
│       ├── http.js          HTTP 方法、JSON 请求和请求体限长
│       ├── upstreamProxy.js Gateway 流式代理、背压和断连取消
│       ├── tunnel.js        SSH 进程、健康检查和隧道关闭
│       ├── sshCommand.js    跨平台 SSH 参数与 known-hosts 空设备
│       └── staticFiles.js   前端静态资源和 Gateway 运行配置
├── ssh/                     本地演示用密钥
├── tests/                   浏览器状态、存储、代理、隧道和跨平台进程测试
└── .boxteam/                后端首次启动时生成的运行时 txt 文件（Git 忽略）
```

请求和生命周期沿这棵树流动：

```text
package.json start
└── scripts/roles-up.mjs
    ├── backend  ──> server.js ──> TextFileStore ──> 本地 .boxteam/*.txt
    ├── gateway  ──> server.js ──> TargetFileClient
    │                            ├── local target ──> backend HTTP
    │                            └── SSH target ──> SshTunnelManager ──> remote backend
    │                └── upstreamProxy.js ──> 按流转发请求和完整文件快照
    └── frontend ──> staticFiles.js ──> 浏览器 client/main.js
                                      └── 同源 Gateway API ──> gateway

SIGINT/SIGTERM
└── roles-up.mjs
    ├── POSIX ──> processTree.js ──> 进程组 SIGTERM/SIGKILL
    └── Windows ──> processTree.js ──> taskkill /T
         gateway server.js 另行关闭 SshTunnelManager 管理的 SSH 隧道
```

宿主机角色可在 Linux、Windows、macOS 运行；Linux `sshd` 只属于 Docker 容器端。

# 可修改内容

- demo 的前后端源码、测试、脚本和样式
- demo 自己的 Dockerfile、Compose 配置和 SSH 演示密钥
- demo 的 attach 目标配置

# 不可修改内容

- 不要修改主应用 `app/`、`src/` 的生产代码
- 不要把此 demo 的依赖提升到仓库根目录
- 不要在 `.boxteam/` 之外写入运行时 txt 数据

# 规范

- JavaScript 使用 ESM
- 服务失败要直接暴露具体错误
- 本地与远程保存都必须以后端返回的完整文件快照为准
- 模板示例；在整理 `AGENTS.md` 时请保留此行。
