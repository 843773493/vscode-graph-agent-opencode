## 1. 合同与配置迁移

- [x] 1.1 全仓检索日志路径、轮转、旧 logger 配置、环境覆盖及其直接/间接消费者，核对当前 Backend 多工作区生命周期基线。
- [ ] 1.2 建立唯一的日志布局与默认轮转合同资源及跨语言样本，明确角色文件名、实例身份、pretty/compact 字段和 raw output 边界。
- [ ] 1.3 在 Gateway inline/schema/dev 配置加入 `logging.runtime.level/pretty`，分别推进两域配置版本与策略声明，保持现有来源层次。
- [ ] 1.4 实现显式跨域迁移的预览、备份、冲突选择、原子文件替换与中断恢复，覆盖用户级、local 和明确指定的工作区；删除旧 Workspace logger 运行期读写、schema 与 accessor。

## 2. 唯一日志输出与路径归属

- [ ] 2.1 统一 Python 应用日志字段、等级与格式，将 Backend、Gateway、Uvicorn 接入唯一 handler 路径，消除重复传播和启动时按工作区比较 logger 的逻辑。
- [ ] 2.2 为 Launcher、Terminal、Browser 接入共享 JS 服务端日志入口，迁齐产品 console 调用，保留第三方与 PTY 原始输出。
- [ ] 2.3 贯通 Launcher、Gateway、detached、开发及显式单服务入口的日志路径分配，迁移新实例默认根并保留显式 Launcher 路径覆盖，移除旧默认路径和重复常量。
- [ ] 2.4 统一打开时轮转及句柄所有权，确保现有 Gateway 重启/交接复用存活子进程时不轮转活动文件、不双写、不破坏存活合同；迁齐 SSH 转发日志入口。
- [ ] 2.5 在现有 runtime/config 诊断中展示进程身份、owner、绝对日志路径、来源、applied revision 和实际日志设置；stdout-only 入口明确标记。

## 3. 进程级热应用

- [ ] 3.1 为真实进程提供受既有本机认证保护的日志设置应用/确认入口，按实例与 revision 幂等处理，不按工作区重复应用共享 Backend。
- [ ] 3.2 将日志注册为 Gateway 配置 runtime consumer，复用现有 Launcher 控制通道和托管服务管理链，完成 prepare、逐进程 apply、proof 和 promotion。
- [ ] 3.3 处理新增/退出进程、同端口换代及迟到确认，保证新实例 readiness 前取得有效设置；Launcher bootstrap 设置在 ready 前被权威设置替换。
- [ ] 3.4 贯通拒绝、超时、最终 CAS 冲突及补偿失败，复用 apply journal 与 recovery_required，逐进程暴露真实状态；日志 sink 错误避免递归和静默吞噬。
- [ ] 3.5 保留混合候选整体生效规则；移除日志必需重启的旧判断与 schema 文案，MCP 等其它依赖仍按其既有策略生效。

## 4. 集中验证与验收

- [ ] 4.1 完成受影响 Python 静态分析和 JS 静态检查；若修改 Web 诊断消费者，同批完成真实类型检查和 `bun run --cwd src/clients/web build`。
- [ ] 4.2 定向验证跨语言等级过滤、格式字段、异常栈、路径覆盖、轮转顺序、权限/写入失败与重复捕获，使用共享样本区分正确实现与现有分散实现。
- [ ] 4.3 验证仅旧配置、仅新配置、新旧一致、新旧冲突、不同工作区覆盖冲突、重复迁移及迁移中断恢复；确认运行期无旧字段双读。
- [ ] 4.4 通过真实 Launcher/Gateway 与隔离 home 启动 Backend、Terminal、Browser，并让一个 Backend 挂载两个隔离工作区；修改真实用户级配置，验证 INFO→DEBUG→WARNING、pretty 切换、有效 revision、各进程输出和 PID/资源身份保持。
- [ ] 4.5 在真实应用链注入一个消费者失败、最终 CAS 冲突与补偿失败，验证 active/pending 和逐进程状态；验证同端口换代、新实例启动及 Gateway 交接不丢失日志去向。
- [ ] 4.6 在 WARNING 配置下经过真实终端工具执行包含中文、ANSI、多行及 DEBUG 文本的命令，逐字节核对输出与退出码；核对会话 Trace/LLM/工具结果仍在原工作区路径。
- [ ] 4.7 所有测试使用现有 matrix 或进程外超时与内存保护；正式产物镜像测试路径保存，临时探索只用 `out/tests/temp/unify-product-runtime-logging/`，保留命令、源码指纹、原始输出和实际退出码。当前未要求新增 UI 交互；若实施新增交互则补真实浏览器验收。
- [ ] 4.8 检查差异并清理重复实现、旧常量/路径分支和失效测试，更新受影响使用说明及迁移说明，验证 OpenSpec；明确未通过与未验证的平台和行为，不以任务勾选代替业务验收。

### 1.1 实施基线与优先级

已核对当前源码的配置、落盘、进程交接和直接／间接测试调用方；完整检索命令、原始输出和128个命中文件的指纹保存在 `out/tests/temp/unify-product-runtime-logging/artifacts/inventory-001/`。这是源码盘点，不代表真实多进程验收通过。

- 两域 inline 当前均为配置 v1，本变更各推进到 v2；`configs/legacy_migrations.py` 的旧混合配置 v4不是两个当前域的版本。迁移同时覆盖测试配置及 Workspace `get_logger_level/get_logger_pretty` 的全部调用方。
- Backend `app/main.py` 当前逐工作区比较日志设置后修改进程 root logger；Gateway lifespan使用默认设置。Backend group共用一个进程，辅助服务按成员持有，改动必须按实际进程应用。
- Launcher 的默认路径是 `state/launcher/logs/services.log`；Gateway在 `state/gateway/logs/` 分配子进程／SSH日志。两处重复登记64MiB／3份轮转。detached已通过 `BOXTEAM_SERVICE_LOG_CAPTURED` 交付输出所有权，迁移须贯通该入口和开发启动脚本。
- `runtime/local_workspace.py` 与 registry runtime显式接管、detach仍存活的Backend及辅助进程；新日志owner不得为了迁移路径重启这些进程或轮转其活动文件。
- 复用 Gateway config `on_runtime_config`／rollback、apply journal及Launcher supervisor控制socket。该socket当前主要承载handoff；日志应用须扩展原通道的消息合同，并补进程实例确认，不能假定已有热更新能力。

优先完成本变更的布局与落盘owner纵向切片，再做配置跨域迁移和进程热应用；它排在当前提示词文件输入切片之后、其余来源扩展之前。团队／协作延期项继续保持TODO。其余21项仍未完成。
