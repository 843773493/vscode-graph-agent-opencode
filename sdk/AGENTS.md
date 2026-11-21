# 目录用途

提供 Node 与 Python 插件共享的独立运行库及生成协议；仓库内产品开发分别使用 Bun workspace 与 uv 标准 editable 安装，插件和产品发行消费构建包。

# 可修改内容

- 语言 SDK 的公开入口、公共运行库、维护调度与包构建声明。
- 从 `proto/` 生成的协议与从唯一静态源构建的只读包资源。

# 不可修改内容

- 不迁入 Gateway 安装管理、工作区业务 owner 或具体插件功能。
- 不依赖产品源码、客户端类型或开发机路径，不保留旧入口转发。

# 规范

- 软件版本由根 `package.json` 在准备／构建时同步，生成协议不手改。
- 根入口保持轻量，HTTP、日志与诊断由对应公开子模块显式使用。
- 包资源及第三方依赖随制品交付，安装和启动不补装依赖。
- 共享实现测试分别位于 `tests/unit/sdk/{python,node}`、`tests/contracts/sdk/{python,node}` 与 `tests/integration/sdk/{python,node}`；产品消费者验收留在产品 owner 目录。新增 SDK 模块须迁齐对应测试与矩阵，不能只修改旧 app 测试导入。
