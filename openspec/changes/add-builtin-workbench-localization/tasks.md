## 1. 固定输入与迁移范围

- [x] 1.1 全仓检索现有语言偏好、显示语言动作、启动入口、硬编码文案及直接／间接消费者，按 design D5 核对真实交付面；协调共享源码转换文件的写入方。
- [x] 1.2 固定 Code OSS 与中文翻译数据输入，登记来源、版本及许可，明确有效源码提取和项目补译归属；不依赖 reference_repo、已安装 VSIX 或构建时临时下载。

## 2. 启动与原版 NLS 单链路

- [x] 2.1 在现有源码转换／构建接缝复用上游 NLS 分析及格式化，提取转换后的有效模块；核对项目改变语义的 key、localize2 英文 original 与占位符。
- [ ] 2.2 生成本地中英文资源和同批消息映射，构建时拒绝产品缺译／重复冲突／占位不符，披露上游缺译并补齐核心验收界面；沿现有资产身份配对资源，不新增运行期完整性扫描。
- [x] 2.3 将真实入口改为先读取原语言偏好、准备资源及 HTML lang，再动态导入业务主入口，迁齐 Web／Electron renderer／parity 的静态导入旁路；默认和清除均使用中文。
- [x] 2.4 从同一文案源提供最小启动提示及恢复操作，明确处理不支持的偏好、资源加载失败和错版；保留原始详情，不静默换语言或扫描用户数据。
- [x] 2.5 将原 Locale／LanguagePack 服务接到内置中文与 English，删除已迁齐的市场发现、语言包安装和旧语言选择旁路，保留原命令身份及行为。

## 3. 产品文案与格式化迁齐

- [x] 3.1 在 shared/core/i18n 建立启动内固定的纯翻译／格式化入口和产品唯一英文原文、稳定 key、中文资源；生成 Workbench／React 所需投影，不新增语言存储、订阅或跨根 Provider。
- [ ] 3.2 迁齐 Workbench 项目贡献的导航、Tab、命令、菜单和设置说明；保持命令／配置／URI 身份和快捷键不变。
- [ ] 3.3 迁齐 React 会话、消息操作和输入栏文案，包括 Tooltip、ARIA、对话框与空态；删除已替换硬编码调用，不改用户及模型内容。
- [ ] 3.4 迁齐会话文件／更改、Trace／请求详情、任务／Goal、调试和底部面板文案，派生状态在展示时翻译，不重写历史镜像。
- [ ] 3.5 迁齐 Gateway／工作区、连接和插件管理产品文案；已识别错误按现有机器身份显示摘要并保留详情，未知错误原文呈现，不扩展后端错误协议。
- [ ] 3.6 统一实际使用的日期、数字、计数、时长和相对时间格式化，清除固定 zh-CN 或浏览器语言旁路；保留时区、数值、用户名称及原始工具／终端／日志内容。

## 4. 内置功能与构建资源

- [ ] 4.1 为实际交付内置扩展同时供应清单翻译与运行期 l10n，接入原 scanner／资源 loader；不以名称翻译替代完整功能验收，不恢复用户 Extension Host。
- [ ] 4.2 迁齐实际有文案的 Webview／Worker 入口和晚创建实例，使其加载前取得本次语言及本地资源；保持原隔离和 URL 边界，无文案 Worker 不添加协议层。
- [ ] 4.3 在 Vite transform、optimizer、esbuild 主包及相关 Worker 复用同一提取规则和一致映射，翻译与规则变化纳入原失效条件；不要求清浏览器数据，不实现无刷新语言切换。
- [x] 4.4 使 Web／Electron 正式产物包含双语及实际内置功能资源，验证源码外本地供应和原静态缓存身份；不增加 CDN、翻译后端或独立资源缓存系统。

## 5. 选择、重载与状态保护

- [x] 5.1 在原 Quick Pick 实现双语选项、当前项、选择／取消／清除／存储失败及重载提示；取消即时重载保留当前界面语言，新偏好供下次启动读取，不建立 pending 账本。
- [ ] 5.2 将语言重载接到原 host 重载、脏文件保护及展示存储提交，核实 Composer 草稿、历史镜像和视图 owner 均已结清；提交失败或取消保护不得强制刷新，不增加语言专属快照。
- [x] 5.3 验证并补齐真实 Electron renderer 重载路径的原存储提交，不能假定窗口 close 保护自动覆盖 reload；保持原 BrowserWindow／main 和后台业务服务，不新增原生语言配置或 IPC。

## 6. 集中验证与最小实现复审

- [x] 6.1 增加少量有判别力的定向合同测试：默认／清除／不支持值、偏好写入失败与取消、localize2 original、占位冲突、资源映射及首次模块求值顺序；沿真实入口，不只测翻译 helper。
- [ ] 6.2 完成相关静态分析、真实类型检查及 `bun run --cwd src/clients/web build`，并执行受影响 Workbench 开发／正式构建；同源码已完成构建可复用，不默认全仓测试。
- [x] 6.3 复用正式真实 Web 装具，验证英文浏览器无偏好默认中文→选择英文并重载→清除回中文；经过实际 Gateway／Workspace 初始化，覆盖首次命令／设置和 React 双语，不使用预写缓存或响应替身代替此链路。
- [ ] 6.4 在上述场景合并验证多 Thread／分屏、预览与固定 Tab、Composer 草稿及脏文件保护，覆盖取消和提交失败；核原文内容、Thread owner、后台执行身份及无重复操作，保留未覆盖范围。
- [ ] 6.5 验证实际交付内置编辑器／预览及晚创建内容双语、无市场／翻译外网请求；核 Vite 资源更新后无需清缓存，正式资源升级不串用消息索引。
- [ ] 6.6 从按仓库版本约定构建的源码外真实 Electron 产物，验证无开发服务器／外网翻译时启动、双语切换、同 renderer 重载和退出恢复；连接已有 Gateway，不以 electron-web 作为原生验收。
- [ ] 6.7 复用现有交互性能场景，在两种语言下核普通暖 API p95≤500ms／单次≤1500ms、导航总 p95≤1000ms／单次≤2000ms、会话 Composer 及文件正文每次打开≤2000ms；更严格历史预算保持，冷启动／重载单列到可操作时点。记录本地语言选择器耗时及零翻译网络请求；按原合同采样，保留规模、原始样本、p95／max、适用 request_id 和未验范围，不新增生产监控或重复长跑。
- [x] 6.8 所有测试使用进程外超时与内存保护，沿既有 out 产物路径保存命令、源码指纹、原始输出、退出码和失败阶段；超预算先归因，不放宽断言、改环境或丢弃慢样本。
- [ ] 6.9 复审并删除本次新增冗余、重复状态、语言旁路、旧调用方及无必要兜底，核文案 owner／偏好单一、无热切换框架、无运行期全包扫描；更新任务与实际验证证据后再申请归档。

### 已完成切片的验证证据（2026-11-12）

- 固定 Code OSS `fc8954c90fe6dea1f4c411dd73fdfe5f1913b5ca` 与 vscode-loc `62d5443b567022539ba6e8565b0f43f5f6b3682b`，来源及许可位于 `src/clients/workbench/resources/localization/`，构建不联网。有效源码 NLS 分析复用固定上游分析器，保留 localize2 original；定向合同 9/9。
- 启动浏览器合同 6/6：默认／显式英文、首次业务求值之前设置资源、只请求当前全文、不支持值、具名 Storage 与资源加载失败。语言服务合同 4/4：当前语言无写入、取消重载、写入失败、清除默认及真实 restart／reload 展示提交失败传播；这些服务依赖装具不替代完整页面或原生 Electron。
- 冻结目录 `out/tests/temp/2026/11/12/workbench_i18n_core/artifacts/final-freeze/` 保留已通过的真实类型检查与正式 Workbench 构建；该产物的 748 项上游缺译报告仍不足以证明全部模块提取。后续真实 Quick Pick 暴露晚置 import 被预筛漏掉，已删除重复绑定解析，沿上游分析器修复；最新类型／构建与受影响产品场景继续验证，不能沿用旧报告宣称完成。
- 迁译模块的定向合同及真实页面原件位于 `out/tests/temp/2026/11/12/workbench_i18n/artifacts/`，导航及底部面板分别沿对应 agent 产物保留；均使用进程外超时及内存保护。
- 真实 Gateway／Workspace 与正式 Workbench 的语言场景通过：`test_localization` 的 `run-805b32f34f6d`，1 passed／37.56 秒；消费 `block-restore-notification/dist` 的冻结产物。无偏好英文浏览器默认中文，原命令切换英文、清除回中文；具名偏好写入失败通过原通知显示路径及 `{0}` 原文，偏好不改且零重载，取消同样零重载。两 Thread 草稿、dirty 文件、分屏和后端身份均保持；未据此声称运行中后台任务已验。选择器 5 次为 53–117ms，冷启动 4.147s，英文／中文重载分别 2.501s／1.852s；完整交互性能与 Electron 仍未验。
- Thread 宿主与恢复链迁译后，真实 `ThreadInput`／`ThreadSerializer` 及原只读模型合同 15 passed／271 断言，日志 `out/tests/temp/2026/11/12/workbench_i18n/artifacts/thread-host-contract-001.log`；用户标题、已存错误、JSON 原件、URI 及分屏展示归属保留。该检查不替代最新前端构建或真实 Electron。
- 本批未勾选项仍按完整条目继续实施／验收；运行中后台任务、晚创建内容、Vite 更新、完整交互预算及源码外 Electron 不由上述局部合同替代。
- 项目转换中的插件视图／命令和图片身份错误已迁齐英文 original。真实 Quick Input 两模块的晚置 NLS import 已纳入定向合同；最新 NLS 提取／资源合同 15 passed／59 断言，日志 `out/tests/temp/2026/11/12/workbench_i18n/artifacts/nls-extraction-final-002.log`。
- 实际 bootstrap 合同已迁齐 bundle 绑定的内容地址 manifest；默认／英文、首次求值、偏好读取拒绝、未知语言恢复、catalog／NLS 加载失败、批次错配及缺少 contents 共 8 passed／37 断言，原始详情保留，日志 `out/tests/temp/2026/11/12/workbench_i18n/artifacts/bootstrap-final-001.log`。
- 文件入口／资源动作／来源选择／Thread 引用各文件独立受保护执行，122 passed；自动化 21 项中 20 项先通过，凭据失效项修正原本错误的提示匹配后单例通过，零写入断言保留。发布指纹补入共享客户端词典来源，相关 63 项合同通过；Electron 旧编译 metadata 消费者已迁齐，最终归档合同 2 passed／20 断言。真实 Electron 语言重载仍在验收，不以这些合同代替。

- 本次提交前的实际类型检查已通过，覆盖 182 份转换后源码，日志 `out/tests/temp/2026/11/12/workbench_i18n/artifacts/precommit-types-001.log`；`bun run --cwd src/clients/web build --outdir .../workbench_i18n/precommit/dist` exit 0，当前产品双语各 3783 key、23180 条有效 NLS 消息、79 个内置扩展。4161 项上游缺译仍明确列在产物报告中，媒体预览等核心缺项继续补译，不以本次构建代替完整覆盖。
- MCP、端口、只读来源／更改、Debug、shell 和外观文案已补齐到本轮目录；相关双语／原文合同 45 passed／285 断言，MCP 转换及语言资源合同 49 passed／336 断言。复审恢复四个 Debug WebSocket close frame 的固定原因，不因语言改变协议值，并删除对应无调用词条。现有未知服务错误、用户标题、工具结果及历史原件保持原文。
- Linux 原生 Electron `test_localization` 的 `run-602a5c25834c`，1 passed／46.51 秒：源码外真实 ASAR、独立 cwd、空 PATH、英文系统环境，覆盖中英文切换与清除、同窗口重载、两 Thread 草稿、两组脏文件及正常关窗重开；两次原生进程均退出 0，后台身份保持，零模型调用和外网翻译请求。结果位于 `out/tests/integration/clients/electron/test_localization/runs/run-602a5c25834c/nodes/node-2fe436c6f4ec/artifacts/localization.json`。这是 0.0.2 开发验证包，尚不替代任务 6.6 规定的干净版本化发行验收，也未覆盖活动执行与完整性能预算。
- 构建的语言身份已纳入全部实际转换脚本与登记的转换源码字节，避免遗漏 MCP 等子脚本；资源仍在构建时确定，启动不增加 hash 扫描。最终 NLS／提取合同 15 passed／59 断言，日志 `out/tests/temp/2026/11/12/workbench_i18n/artifacts/precommit-nls-001.log`。
