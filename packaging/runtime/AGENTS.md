# 目录用途

本目录维护自包含平台运行时的 staging、npm 包组装、构建输入指纹和下载实现；安装验收实现位于 `verification/`，发布命令入口位于 `scripts/release/`。

```text
runtime/
├── versions.mjs                   发布版本及第三方固定资源清单
├── build-input-fingerprint.mjs    构建输入指纹，复用64KiB缓冲区读取正文
├── build-input-fingerprint.test.mjs 指纹格式、shared源码、发布边界与短读取
├── runtime-build-result.mjs       两平台构建版本快照、失效及metadata发布owner
├── runtime-release-assets.mjs     runtime/Launcher包组装、manifest、下载器及体积报告
├── pinned-archive-download.mjs     固定归档下载、续传与摘要校验，显式接收下载根
├── pinned-archive-download.test.mjs 构建归档下载、缓存、续传及文件边界
├── build-archive-downloads.test.mjs 构建入口选择下载owner、目录及顺序
├── build-{linux,windows}-x64.mjs   各平台staging与构建
├── application-sources.mjs         平台runtime共用的应用源码复制owner
├── application-sources.test.mjs     实际复制清单、字节码过滤及文件字节
├── windows-python-dependencies.mjs Windows Python锁定依赖、交叉安装及残留校验
├── windows-python-dependencies.test.mjs 命令/文件边界与实际构建入口接线
├── verification/                  两平台安装布局、relocation及产品验收
│   ├── AGENTS.md                  安装验收目录职责与规则
│   ├── verify-{linux,windows}-x64.mjs 平台安装验收入口
│   ├── verification-readiness.mjs Gateway健康探针与等待预算
│   ├── verification-readiness.test.mjs 本地HTTP及Launcher就绪边界
│   ├── verification-shutdown.test.mjs Windows Launcher停止观察边界
│   └── verification-shutdown-timers.test.mjs 两平台关闭计时器边界
└── runtime-release-assets.test.mjs 发布版本、平台依赖、体积报告及真实builder接线
```

先区分版本事实、包组装、平台构建和产物验证，再进入对应入口；测试中的最小包目录不是可发布runtime，也不承担业务工作区生命周期。

发布资产测试只创建最小npm包、runtime组件树及归档替身，不创建业务工作区。输入与原始结果分别保留在`out/tests/packaging/runtime/runtime-release-assets/{workspace,artifacts}/<run_id>/<scenario>/`；运行及场景目录独占创建，不依赖系统临时目录，不在结束时删除现场。Node原始stdout、stderr及退出结果先保存再解析，不执行打包、下载或安装。Launcher接线场景只复制Launcher源码包，执行builder全文至第一次npm pack之前；下载、依赖安装、应用复制和外部命令是替身，包复制、版本注入、metadata与staged Launcher的`--version`使用真实实现。

```text
runtime-release-assets/
├── workspace/<run_id>/
│   ├── runtime-manifest-version/待发布 package/        runtime版本注入
│   ├── launcher-platform-dependencies/待发布 package/  Launcher平台依赖
│   ├── package-staging-{linux,windows-cross,windows-native}/  Launcher最小构建现场
│   └── size-{linux,windows}/                          对应平台的体积报告输入
│       ├── runtime package/                          四种组件的多层最小文件树
│       └── 归档 输出/                                归档替身与size-report.json
└── artifacts/<run_id>/
    ├── staging-observer.mjs                          本次构建接线的共用观察入口
    ├── package-staging-<mode>/                       input、stage观察、CLI及Node原始结果
    └── size-{linux,windows}/                         体积报告Node原始结果
```

Linux/Windows构建入口只传runtime根、输出根、npm tarball和具名发布资产分组，体积枚举与报告写入由`writeRuntimeSizeReport`统一负责。报告只序列化一次，文件与stdout复用同一字节串；原JSON分组和输出格式保持。`components`累加各组件中非目录条目的`stat().size`，保留文件链接的目标长度、目录链接不递归的既有语义；它不是磁盘实际占用或去重后的归档体积。测试覆盖中文/空格、多层目录、空文件及平台分组，并禁止通过整份读取或流读取组件正文来统计大小。

构建输入指纹由Linux/Windows构建和本地安装复核共用，包含浏览器bundle会引入的`src/shared/`；按既有排序依次写入路径、NUL分隔符、正文或链接目标，不改变同步接口或摘要格式。顶层输入用`lstat`识别类型，后代复用`readdir({withFileTypes:true})`返回的`Dirent`，不重复查询同一条目的类型；链接只记录目标，不递归跟随。文件正文只复用一个64KiB缓冲区，目录清单仍保存在内存中；读取异常也须关闭本次描述符，不能将“正文有界”写成整个算法内存恒定。`runtime-build-result.mjs`在输出被替换前清除旧成功metadata，发布前复核源码仍与起始快照一致，并将该快照写入metadata。

`runtime-release-assets.mjs::stageRuntimeAndLauncherPackages`是两平台runtime manifest与Launcher staging的唯一owner。Launcher排除任意层级的`node_modules`及普通`AGENTS.md`、`*.test.mjs`文件；生产入口、schema和其它文件保留原字节，版本与平台依赖继续在staging注入；staged manifest去掉已无对应测试文件的`test`脚本，其它脚本保留，源码包测试入口不变。Windows便携版沿用这份Launcher staging，不再复制开发文件；npm命令和Windows shell参数仍归平台builder。

Windows 便携版复制 Launcher 后只移除其顶层 runtime optionalDependencies，再按 win32/x64 安装生产依赖；保留日志锁依赖的 optional 平台原生包，不能全局 omit optional。npm 安装版继续由包管理器安装 Launcher 的正式依赖。

构建指纹只在`packaging/runtime/`与`packages/launcher/`子树排除普通`AGENTS.md`、`*.test.mjs`文件，前者不进入发行包，后者已在staging排除。同名目录及链接保持既有语义；其它输入根中的说明与测试仍可能随源码进入产物，继续计入。测试同时证明排除文件变化不触发重建、目录内产品正文及builder修改仍改变指纹；不能把排除扩展到全仓。

构建输入的相对路径在枚举阶段统一用`/`，用于排除规则、全局排序和hash标签；实际打开文件时才用宿主`node:path`定位。不能先按Windows反斜杠排序、最后才替换分隔符：目录前缀相同的`a/文件`与`a0.py`会因此交换顺序，使相同普通文件树产生不同指纹。目标发布平台与宿主路径是两个独立维度；逻辑路径归一保留Linux/macOS既有排序规则，旧Windows构建缓存可能需要重新构建，不保留第二套摘要算法。

指纹测试按`out/tests/packaging/runtime/build-input-fingerprint/{workspace,artifacts}/<run_id>/<target_platform>/<scenario>/`保留最小源码树与原始结果；运行ID和场景独占创建，Node的stdout、stderr、真实退出状态在解析结果前保存。目标平台决定构建输入，真实文件路径仍遵循宿主规则；场景名称区分指纹格式、来源变化、发布边界和读写故障，不能把最小源码树当作可发布runtime。固定指纹覆盖中文、空格、空文件、跨块正文与忽略目录；独立Node记录目录枚举和类型查询次数，并在文件边界限制读取、注入短读取和异常。Linux/macOS的链接用例核文件链接、目录链接和悬空链接，不安装依赖、不构建发行包，也不证明Windows/macOS原生文件系统验收。

```text
build-input-fingerprint/
├── workspace/<run_id>/<target_platform>/<scenario>/源码 工作区/  输入及变更后的现场
└── artifacts/<run_id>/<target_platform>/<scenario>/             子进程观察原件
    ├── stdout.raw
    ├── stderr.raw
    └── result.json
```

先按用途分开输入与观察，再用相同的运行、目标平台和场景定位两者；运行ID防覆盖，场景名解释测试目的。直接调用指纹的场景只保留输入现场，独立Node观察场景同时保留进程结果。

`cross-host-paths`场景对同一份磁盘fixture执行完整指纹模块，分别注入`path.posix`和`path.win32`，仅将文件入口映射回真实目录；记录两种规则下的打开顺序及摘要，并与原生调用比较。fixture包含前缀相同的目录/文件、中文和空格，测试的是路径排序合同，不代表原生Windows文件系统、大小写或符号链接验收。

`pinned-archive-download.mjs`拥有固定归档的下载、缓存和续传；各平台构建入口只负责选择具名函数并传入下载根，不在下载owner中读取环境或构建staging路径。固定Python/Node归档的缓存及下载后校验共用`runtime-release-assets.mjs::sha256File`，按流读取，不整份加载归档。Linux Python保留无效缓存拒绝，Windows Python保留Range续传及416后的最终摘要拒绝，Windows Node保留无效缓存重下。

下载正文由`Readable.fromWeb`接Node的`pipeline`写盘，流处理短写和背压；每次请求结束取消fetch信号，HTTP拒绝或文件打开失败也释放未消费正文。Linux Python删除本次失败创建的部分归档，防止下一次将其当作缓存；清理同时失败保留下载和清理两个原因，不删除已有无效缓存。Windows Python的FileHandle写入流采用默认自动关闭，finally再关闭句柄是幂等兜底；不能设autoClose=false后直接等待FileHandle.close，实际探针会悬住。续传偏移读取实际文件长度，不能累加响应chunk长度；最多5次尝试、每次120秒和一秒退避保持，资源先释放再退避。Linux Python仍未新增下载期限，Windows Node保持120秒，不混用fork业务期限。

`pinned-archive-download.test.mjs`在正式镜像输出的独立`workspace/<run_id>/<archive_kind>/<scenario>/`中执行下载owner全文；版本资源指向随机本地HTTP，磁盘、流和摘要使用真实实现。文件边界注入短写/失败，核请求连接释放；Undici补建但未承载请求的空闲连接不属于该断言，fixture结束仍关闭全部连接。故障场景只压缩一秒退避，不改真实请求或fixture期限。

`build-archive-downloads.test.mjs`在自己的正式镜像输出根执行构建入口全文，下载边界记录所选owner及目录，首个解包命令处主动结束；覆盖Linux、Windows原生及Linux交叉构建分支的默认根和独立下载根。平台为显式替身，不启动真实子进程。两层测试均不访问发布服务、安装依赖或执行打包，不证明真实归档可解包或Windows/macOS原生构建成功。

两组下载测试都按用途、运行、平台和场景保留正式fixture，结束后只关闭自己的HTTP资源，不删除输入树与结果；运行ID和场景目录独占创建。每次运行只保存一份`observer.mjs`，各场景的`input.json`记录模块及调用参数；Node直接执行这份脚本。原始stdout/stderr和实际退出状态先保存，再判断成功或解析，失败信息指向对应artifacts，不能只留下聚合测试日志。

```text
out/tests/packaging/runtime/
├── pinned-archive-download/
│   ├── workspace/<run_id>/<linux-python|windows-python|windows-node>/<scenario>/  归档与最小版本输入
│   └── artifacts/<run_id>/
│       ├── observer.mjs                   本次运行共用的独立Node观察入口
│       └── <archive_kind>/<scenario>/     input.json、stdout.raw、stderr.raw、result.json
└── build-archive-downloads/
    ├── workspace/<run_id>/<linux|windows-cross|windows-native>/<scenario>/  最小源码与staging现场
    └── artifacts/<run_id>/
        ├── observer.mjs                   本次运行共用的独立Node观察入口
        └── <mode>/<scenario>/             input.json、stdout.raw、stderr.raw、result.json
```

输入与观察先分开，同一运行ID、平台和场景连接两者；场景名说明默认目录、独立下载根、缓存、续传或故障目的。`result.json`保存准确命令、cwd、5秒子进程上限及真实退出原因；外层测试仍需进程外内存与超时保护。现场不是可发布runtime或可复用业务工作区；复查原件时不在原目录重跑以覆盖归档状态。


`application-sources.mjs`统一Linux与Windows runtime的源码复制清单，显式接收项目根和application目录，不从文件位置推导根目录；复制`app/`、`configs/`、`resources/`、terminal/browser服务端与各自protocol、`terminal/shared/`终端服务端相对导入模块、`src/shared/`跨进程协议与服务端日志模块、共享protocol及`pyproject.toml`，过滤任意层级的`__pycache__`和`.pyc`。`application-sources.test.mjs`验证最小源码树的文件字节、中文和空格路径、源树不变及过滤；快照键统一使用`/`，真实读写路径仍遵循宿主规则，同一已读快照供相关断言复用。Windows依赖接线测试继续执行真实复制至依赖owner边界。测试不下载、安装依赖或执行发行构建。

```text
out/tests/packaging/runtime/application-sources/
├── workspace/<run_id>/
│   ├── source-integrity-and-bytecode-filter/  最小源码树与复制结果
│   └── terminal-import-closure/              终端依赖闭包的源码与staging
└── artifacts/<run_id>/
    ├── source-integrity-and-bytecode-filter/  源树前后快照与复制结果快照
    └── terminal-import-closure/              stdout.raw、stderr.raw、result.json
```

先按输入与观察结果分层，再用同一运行ID和场景连接两者；运行和场景目录独占创建，正式fixture及结果保留，子进程原始结果先落盘再判断成功。只有终端闭包场景复制真实终端模块并执行Node import，不启动终端、业务服务或安装依赖；本机通过不能替代Windows/macOS原生验证。

`verification/verify-linux-x64.mjs`与`verification/verify-windows-x64.mjs`共用`verification-readiness.mjs`，各入口显式传入Launcher、健康URL、认证头及原有60/180秒预算；Windows便携版使用自己的Launcher句柄。请求、重试与迟到响应共用单调期限，状态码探针取消未消费正文及传输，失败保留最后原因，成功交付前复核进程退出码和终止信号；观测超时不表示产品已停止或回滚。Windows停止等待、产品API验收与进程终止仍归各自验证入口，不混入就绪owner。

就绪测试只在`out/tests/packaging/runtime/verification/verification-readiness/workspace/<run_id>/<case_id>/`中启动独立Node和随机本地HTTP，Launcher状态为替身；覆盖挂起请求、未结束正文、503重试、信号退出、迟到成功与墙钟跳变。fixture清理本次连接，保留场景目录及对应的原始stdout/stderr和result，不操作用户端口或服务，不代替发行包安装与原生平台验收。Windows停止观察测试使用`out/tests/packaging/runtime/verification/verification-shutdown/workspace/<run_id>/`；计时器测试在相应`verification-shutdown-timers/{workspace,artifacts}/<run_id>/<scenario>/`保留独立Node的可重放输入和原始结果，具体边界见子目录AGENTS。

Windows Python依赖准备由`windows-python-dependencies.mjs`独占：导出锁定依赖→按固定名单分离目标wheel与源码包→安装目标依赖→替换源码包→删除这些源码包的Linux扩展→拒绝其它Linux扩展残留。构建入口传入Python位置、输出根、runtime根、交叉模式和原命令runner，owner不推导仓库根或读取环境。原生分支继续直接安装到目标Python，不进入交叉清理；字节码预编译仍由构建编排负责。

交叉清理与残留校验共用一次完整目录枚举，枚举后只删除固定源码包根内的`.so`，其余`.so`保留并统一拒绝；相似包名不获得删除权限，`.pyd`及普通源码不受影响。函数名同时表明“删除源码包Linux扩展”和“拒绝其它扩展”两项效果，不以泛化的validate掩盖文件删除。

```text
out/tests/packaging/runtime/windows-python-dependencies/
├── workspace/<run_id>/
│   ├── dependency-owner/<scenario>/依赖 fixture/  锁文件、最小包树与清理现场
│   └── builder-wiring/<native|cross>/依赖 fixture/ 构建入口的最小源码与staging
└── artifacts/<run_id>/
    ├── dependency-owner/<scenario>/             stdout.raw、stderr.raw、result.json
    └── builder-wiring/<native|cross>/            stdout.raw、stderr.raw、result.json
```

输入与结果先分开，再按相同运行ID、被测职责及场景定位；目录独占创建、正式现场保留，Node原始输出和真实退出状态先保存再解析。依赖测试用命令替身，文件处理仍执行真实owner；独立Node观测完整目录覆盖及枚举次数，验证清理和拒绝结果，不能只用计数证明正确。构建入口测试执行原builder全文核目录、模式及runner接线，在依赖准备前结束；平台选择为显式替身，不下载、安装依赖、编译字节码或执行真实发行构建。

# 可修改内容

- 运行时构建脚本、固定资源清单和 npm 平台包模板。
- 运行时结构与体积报告逻辑。

# 不可修改内容

- 不提交下载后的 Python、Chromium、node_modules 或 tarball。
- 不调用 `npm publish`。

# 规范

- JavaScript 始终使用 ESM。
- Python、Chromium 和 Node 依赖必须从显式版本清单构建；BoxTeam 发布版本必须从根 `package.json` 读取。
- staging 不得引用构建机仓库绝对路径。
- 模板示例；在整理 `AGENTS.md` 时请保留此行。
