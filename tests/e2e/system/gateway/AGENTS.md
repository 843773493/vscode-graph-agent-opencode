# 目录用途

存放 Gateway、多工作区、SSH 转发和测试容器相关的端到端测试与辅助代码。

```text
gateway/
├── gateway_target.py                  SSH 目标路径、POSIX 与 Windows Gateway 生命周期命令
├── gateway_docker.py                  Docker SSH 目标准备与复用
├── test_gateway_docker_routing.py     Docker 目标上的 Gateway 路由 E2E
├── test_gateway_browser_resource.py   跨 Gateway 的浏览器资源 E2E
└── conftest.py                        镜像测试输出、隔离默认工作区的共享 fixture
```

# 可修改内容

- Gateway e2e 测试文件。
- 复用测试 SSH 容器以及启动/停止远端测试进程的辅助代码。

# 不可修改内容

- 不放置与 Gateway 路由无关的浏览器或终端工具测试。
- 不把测试容器运行产物写入项目根目录。

# 规范

- 测试端口必须由 e2e 端口分配工具管理。
- Docker E2E 统一复用一个长期运行的 SSH 目标容器；测试不得按文件创建 Compose project，也不得在结束时关闭容器。
- Docker E2E 工作区固定放在 `out/docker/<相对测试路径>/`，每次测试开始前重置，结束后保留。
- 测试启动的宿主机和容器内进程默认清理；只有单测试显式传入 `--keep-docker-runtime` 时才允许保留。
- `gateway_target.py` 只管理当前 E2E 所属远端目标的进程；启动或清理目标服务前先确认该目标专用于本测试，不能对共享开发目标清空通用服务端口。
