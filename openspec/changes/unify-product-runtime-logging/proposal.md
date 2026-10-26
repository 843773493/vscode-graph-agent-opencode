## Why

当前 Workspace `logger` 只控制 Python Backend，Gateway 使用默认值，Terminal/Browser 使用各自的 console 输出；Launcher 与 Gateway 又分别决定日志路径和轮转。排障时无法用同一个用户级设置调整全产品的诊断日志，日志归属与配置生效范围也不一致。

## What Changes

- 统一 Launcher、Gateway、Backend、Terminal、Browser 的产品诊断日志合同、配置来源、路径解析和落盘职责，复用现有进程监督体系，不新增日志服务进程。
- **BREAKING**：将 Workspace `logger` 迁移到 Gateway 用户级 `logging.runtime`，提供 `level` 与 `pretty`；两个配置域分别升级版本，冲突必须报告，运行期不双读。
- 支持日志等级与格式热更新，由 Gateway 协调实际进程逐项确认；同一 Backend 挂载的多个工作区共用一次应用，不再由工作区争用 root logger。
- 统一默认日志根为 `${BOXTEAM_HOME}/state/logs/`，由启动 owner 分配服务与运行实例路径。保留显式 Launcher 日志路径覆盖，并在诊断中注明来源；旧日志原地保留，新运行实例不继续写旧默认路径。
- 明确统一的是诊断日志；Shell 命令输出、网页 console、会话 Trace、LLM 请求记录和工具结果保持各自的数据合同与归属。
- 合并重复路径、默认参数和轮转决策，保持当前打开日志时检查大小并轮转的语义，不承诺运行中文件大小硬上限。
- MCP 连接配置仍按现有重启策略生效，本变更不实现 MCP 热切换。

## Capabilities

### New Capabilities

- `product-runtime-logging`：跨语言诊断日志、单文件写入归属、统一路径、进程级热更新确认、失败反馈和原始输出隔离。

### Modified Capabilities

- `configuration-domains`：产品诊断日志归 Gateway 控制面配置，移除 Workspace 的日志配置权威，定义显式迁移与日志热更新范围。
- `configuration-bootstrap`：版本迁移后不再把日志等级和格式变化归为 Workspace 必须重启；保留 MCP 和其他配置的候选原子性。

## Impact

- 配置：Gateway/Workspace inline、schema、dev 模板、安装迁移及配置策略表；既有 JSONC 覆盖层顺序不变。
- Python：`app/core/logging_config.py`、Gateway 生命周期/子进程日志路径与句柄管理、Backend 启动日志一致性检查和配置应用接口。
- JS：Launcher `logging/service-log.mjs`、监督与 detached 启动入口、Terminal/Browser 服务端日志调用。
- 内部控制：复用 Launcher/Gateway 生命周期控制以及本地托管服务认证边界，增加必要的日志 revision 应用与确认，不增加公开日志采集服务。
- 测试：跨语言合同、配置迁移、真实多进程热更新、写入失败、重启/交接及 Shell 输出保持验证；不改变业务数据存储。
- 与进行中的 `add-multi-workspace-backend-mounting` 对齐 Backend 进程身份和完整组生命周期，不修改其规划或实现。
