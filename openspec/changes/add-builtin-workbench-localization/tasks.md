## 1. 固定输入与迁移范围

- [x] 1.1 全仓检索现有语言偏好、显示语言动作、启动入口、硬编码文案及直接／间接消费者，按 design D5 核对真实交付面；协调共享源码转换文件的写入方。
- [x] 1.2 固定 Code OSS 与中文翻译数据输入，登记来源、版本及许可，明确有效源码提取和项目补译归属；不依赖 reference_repo、已安装 VSIX 或构建时临时下载。

## 2. 启动与原版 NLS 单链路

- [x] 2.1 在现有源码转换／构建接缝复用上游 NLS 分析及格式化，提取转换后的有效模块；核对项目改变语义的 key、localize2 英文 original 与占位符。
- [x] 2.2 生成本地中英文资源和同批消息映射，构建时拒绝产品缺译／重复冲突／占位不符，披露上游缺译并补齐核心验收界面；沿现有资产身份配对资源，不新增运行期完整性扫描。
- [x] 2.3 将真实入口改为先读取原语言偏好、准备资源及 HTML lang，再动态导入业务主入口，迁齐 Web／Electron renderer／parity 的静态导入旁路；默认和清除均使用中文。
- [x] 2.4 从同一文案源提供最小启动提示及恢复操作，明确处理不支持的偏好、资源加载失败和错版；保留原始详情，不静默换语言或扫描用户数据。
- [x] 2.5 将原 Locale／LanguagePack 服务接到内置中文与 English，删除已迁齐的市场发现、语言包安装和旧语言选择旁路，保留原命令身份及行为。

## 3. 产品文案与格式化迁齐

- [x] 3.1 在 shared/core/i18n 建立启动内固定的纯翻译／格式化入口和产品唯一英文原文、稳定 key、中文资源；生成 Workbench／React 所需投影，不新增语言存储、订阅或跨根 Provider。
- [x] 3.2 迁齐 Workbench 项目贡献的导航、Tab、命令、菜单和设置说明；保持命令／配置／URI 身份和快捷键不变。
- [x] 3.3 迁齐 React 会话、消息操作和输入栏文案，包括 Tooltip、ARIA、对话框与空态；删除已替换硬编码调用，不改用户及模型内容。
- [x] 3.4 迁齐会话文件／更改、Trace／请求详情、任务／Goal、调试和底部面板文案，派生状态在展示时翻译，不重写历史镜像。
- [x] 3.5 迁齐 Gateway／工作区、连接和插件管理产品文案；已识别错误按现有机器身份显示摘要并保留详情，未知错误原文呈现，不扩展后端错误协议。
- [x] 3.6 统一实际使用的日期、数字、计数、时长和相对时间格式化，清除固定 zh-CN 或浏览器语言旁路；保留时区、数值、用户名称及原始工具／终端／日志内容。

## 4. 内置功能与构建资源

- [x] 4.1 为实际交付内置扩展同时供应清单翻译与运行期 l10n，接入原 scanner／资源 loader；不以名称翻译替代完整功能验收，不恢复用户 Extension Host。
- [x] 4.2 迁齐实际有文案的 Webview／Worker 入口和晚创建实例，使其加载前取得本次语言及本地资源；保持原隔离和 URL 边界，无文案 Worker 不添加协议层。
- [ ] 4.3 在 Vite transform、optimizer、esbuild 主包及相关 Worker 复用同一提取规则和一致映射，翻译与规则变化纳入原失效条件；不要求清浏览器数据，不实现无刷新语言切换。
- [x] 4.4 使 Web／Electron 正式产物包含双语及实际内置功能资源，验证源码外本地供应和原静态缓存身份；不增加 CDN、翻译后端或独立资源缓存系统。

## 5. 选择、重载与状态保护

- [x] 5.1 在原 Quick Pick 实现双语选项、当前项、选择／取消／清除／存储失败及重载提示；取消即时重载保留当前界面语言，新偏好供下次启动读取，不建立 pending 账本。
- [x] 5.2 将语言重载接到原 host 重载、脏文件保护及展示存储提交，核实 Composer 草稿、历史镜像和视图 owner 均已结清；提交失败或取消保护不得强制刷新，不增加语言专属快照。
- [x] 5.3 验证并补齐真实 Electron renderer 重载路径的原存储提交，不能假定窗口 close 保护自动覆盖 reload；保持原 BrowserWindow／main 和后台业务服务，不新增原生语言配置或 IPC。

## 6. 集中验证与最小实现复审

- [x] 6.1 增加少量有判别力的定向合同测试：默认／清除／不支持值、偏好写入失败与取消、localize2 original、占位冲突、资源映射及首次模块求值顺序；沿真实入口，不只测翻译 helper。
- [ ] 6.2 完成相关静态分析、真实类型检查及 `bun run --cwd src/clients/web build`，并执行受影响 Workbench 开发／正式构建；同源码已完成构建可复用，不默认全仓测试。
- [x] 6.3 复用正式真实 Web 装具，验证英文浏览器无偏好默认中文→选择英文并重载→清除回中文；经过实际 Gateway／Workspace 初始化，覆盖首次命令／设置和 React 双语，不使用预写缓存或响应替身代替此链路。
- [x] 6.4 在上述场景合并验证多 Thread／分屏、预览与固定 Tab、Composer 草稿及脏文件保护，覆盖取消和提交失败；核原文内容、Thread owner、后台执行身份及无重复操作，保留未覆盖范围。
- [ ] 6.5 验证实际交付内置编辑器／预览及晚创建内容双语、无市场／翻译外网请求；核 Vite 资源更新后无需清缓存，正式资源升级不串用消息索引。
- [ ] 6.6 从按仓库版本约定构建的源码外真实 Electron 产物，验证无开发服务器／外网翻译时启动、双语切换、同 renderer 重载和退出恢复；连接已有 Gateway，不以 electron-web 作为原生验收。
- [x] 6.7 复用现有交互性能场景，在两种语言下核普通暖 API p95≤500ms／单次≤1500ms、导航总 p95≤1000ms／单次≤2000ms、会话 Composer 及文件正文每次打开≤2000ms；更严格历史预算保持，冷启动／重载单列到可操作时点。记录本地语言选择器耗时及零翻译网络请求；按原合同采样，保留规模、原始样本、p95／max、适用 request_id 和未验范围，不新增生产监控或重复长跑。
- [x] 6.8 所有测试使用进程外超时与内存保护，沿既有 out 产物路径保存命令、源码指纹、原始输出、退出码和失败阶段；超预算先归因，不放宽断言、改环境或丢弃慢样本。
- [x] 6.9 复审并删除本次新增冗余、重复状态、语言旁路、旧调用方及无必要兜底，核文案 owner／偏好单一、无热切换框架、无运行期全包扫描；更新任务与实际验证证据后再申请归档。

### 已完成切片的验证证据（2026-11-12）

- 固定 Code OSS `fc8954c90fe6dea1f4c411dd73fdfe5f1913b5ca` 与 vscode-loc `62d5443b567022539ba6e8565b0f43f5f6b3682b`，来源及许可位于 `src/clients/workbench/resources/localization/`，构建不联网。实际 bootstrap 合同 8 passed／37 断言（`bootstrap-final-001.log`），覆盖默认／英文、业务求值顺序、偏好读取失败、未知语言恢复、资源失败与批次错误；实际语言服务合同 4/4 覆盖当前语言无写入、取消、清除及原 restart／reload 提交错误传播。服务装具不替代真实产品验收。
- 有效 NLS 提取复用固定上游分析器，包含晚置 import，保留 localize2 original；资源／提取合同 15 passed／59 断言（`precommit-nls-001.log`）。Thread 宿主与恢复合同 15 passed／271 断言（`thread-host-contract-001.log`）；文件入口／资源动作／来源选择／Thread 引用合同 122 passed。原件位于 `out/tests/temp/2026/11/12/workbench_i18n/artifacts/`。
- MCP、端口、只读来源／更改、Debug、shell 和外观迁译合同 45 passed／285 断言，MCP 转换及资源合同 49 passed／336 断言。四个 Debug WebSocket close frame 的原协议原因保持固定，用户标题、工具结果、未知服务错误及历史原件不翻译。

### 核心界面与失败保护补验（2026-11-12）

- 上游翻译输入恢复为固定来源原件，173 项项目补译独立登记在 `zh-cn-project.json`，涵盖图片／媒体、Diff、搜索、设置及文件对话框；英文仍从实际固定源码取得。最终 `final-closure-types-003.log` 的真实类型检查通过，覆盖 182 份转换后源码；`final-closure-build-003.log` 的正式构建通过，双语产品各 3840 key、23180 条有效 NLS、79 个内置扩展，3988 项上游缺译保留在产物报告中，已无占位符失配。不据此宣称整个上游全部中文。
- 最终产物的真实 Web 完整场景 `run-01dd296b31a2`，1 passed／65.61 秒，外层退出 0，日志 `out/tests/temp/2026/11/12/workbench_i18n/artifacts/real-reload-failure-web-004.log`。原 Quick Pick 写偏好及清除后双向重载；一次实际展示 IndexedDB 写入拒绝会显示具体原因并保持页面，原 beforeunload 取消会保留脏文件。Application scope 的 hotExit 经原用户设置入口保存，未通过工作区配置假定生效。
- 同场景覆盖中英文晚创建 Markdown 的本地扩展资源、图片原预览及缩放选项、两 Thread 草稿、两组脏文件和分屏；原用户正文、路径及 `{0}` 保持。第三 Thread 从 Composer 启动真实后台执行，跨两次语言重载保持同一 Job，释放 model_stream 原响应屏障后完成；消息 POST 和模型调用各一次。页面错误与外网翻译请求均为零。原件位于 `out/tests/integration/clients/web/quick_access/test_localization/runs/run-01dd296b31a2/nodes/node-b75dc0caa814/artifacts/localization.json`。
- 双语现有性能验收 `bilingual-latency-001.log`，2 passed／66.44 秒，run `run-0f6ed1d85da2`。每语 32 Session／128 文件、20 次导航及每 API 20 样本；中文／英文导航 p95 为 145.54／137.56ms，API p95 均不超过 78.9ms，会话 Composer 最大 474.09／535.12ms，文件最大 499.18／392.23ms，原预算未放宽。冷启动和重载单列，页面错误及翻译外网请求为零。
- Linux 原生 Electron 活动执行场景 `run-faad2a9aaace`，1 passed／158.84 秒：源码外 development ASAR、空 PATH、独立 cwd；同窗口双语重载、后台执行只调用一次模型、晚创建 Markdown、正常退出重启恢复均通过。归档 SHA-256 `8709a0c638981c1bc939f87bb11a3303e45b2df34174e3b7b3d4784ebf91f298`；这是 0.0.2 开发验证包，任务 6.6 的版本化发行验收仍未完成。
- 原生新增取消场景 `run-2d0ecc047c9a` 未通过：页面未导航，但第二组 dirty 标签断言失败，失败定位器还记录了等待导航；不能据此认定文件已被保存。该轮仅记录两条 CDP beforeunload，未观察具名 `will-prevent-unload`；清理又因未保存保护超时。现已删除重复的动态 dialog handler，沿 Electron owner 只读观察具名事件，不再用 CDP 操作其自动处理的 beforeunload，严格保留两组 dirty 断言。修改后的原生分支仅通过语法检查，尚未运行；不以 Web 通过替代原生取消验收，TODO 保留。
- Gateway 上传双语合同 16/16、插件 registry 合同 8/8 通过；已知 Activity kind 仅在展示层翻译，未知值、原始错误、用户名称及 wire 身份保留。当前确认对话框也消费唯一产品资源，不建立第二偏好或 Provider。
- 最后补齐轮次定位与历史加载错误包装，原始错误详情保留，轮次数字按当前语言格式化。对应 hook 与 NLS 真实转换／formatter 合同共 36/36 通过，`sourceTransform` 合同 13/13 通过；项目主动改变的插件提示使用独立产品 key，删除了过渡的单调用翻译 helper 与重复判断。全量检索无该 helper 遗留，未新增第二语言状态、运行期覆盖框架或全包扫描。
- 最终独立 Web 类型检查 `bun run --cwd src/clients/web tsc --project tsconfig.json --noEmit` 通过（`final-web-types-006.log/.exit`）；此前暴露的历史缓存测试泛型推导错误只补充 `readBrowserView<string>`，保留原草稿断言，不改运行行为。直接由 Bun 执行 tsc 的尝试退出 132、无编译诊断；原命令与退出码保留，不作为通过。改动 Python 的 Ruff 与全部改动脚本语法检查通过。
- Vite 真实开发场景尚未通过：`test_development_hmr` 的 `run-2a43428d6fc0` 在 optimizer bundling 阶段触发 `std::bad_alloc`，未到浏览器；只关闭压缩的独占源码快照对照仍发生内存分配失败。两次均保留 4 GiB 进程外保护，没有提高额度、放宽断言或要求清缓存。源码 TODO 保留，任务 4.3、6.2、6.5 不据正式构建通过而勾选。
