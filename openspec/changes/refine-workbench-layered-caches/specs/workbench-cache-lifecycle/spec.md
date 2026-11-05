## ADDED Requirements

### Requirement: 缓存必须按原服务分层管理

工作台 SHALL 按 design 的分层矩阵清点并接入模型、文件观察值、资源字节、搜索、导航／目录镜像、聊天历史镜像、配置派生、实际语言／Worker、已启用扩展元数据、静态资源与引擎代码缓存。每层 SHALL 明确原 owner、对象、身份／版本、存储、共享／释放、失效、容量／淘汰及验收入口；MUST NOT 建立统一缓存引擎、全局字节库或为清单每行强造缓存。原 owner 没有该缓存或对应服务未启用时 SHALL 明确不适用，不能以源码存在声称已接通，不新增原生扩展／语言服务体系。

缓存 SHALL 只作为原来源的派生结果，失效／损坏／存储失败 SHALL 有原本地诊断并由对应 owner 处理，不伪报命中或业务成功。诊断 SHALL 区分命中、再验证、未命中、stale、绕过及未支持等适用状态，保留真实范围和已有版本／请求信息；无网络读取不得伪造 request_id。不新增遥测数据库、后台跨工作区扫描或无界预热。

#### Scenario: 某层没有实际服务或缓存

- **WHEN** FileService 的实际 Provider 没有 stat 缓存，或工作区语言／原生扩展缓存对应服务没有启用
- **THEN** 前者保留真实轻量读取，后者明确不适用或未启用；不新造缓存、索引或原生服务凑齐功能清单，不显示虚假命中

### Requirement: 编辑器模型共享必须保持恢复与版本归属

相同规范可变文件 SHALL 沿原 TextFileEditorModelManager／resolver 管理一个 working copy，并发 resolve 复用在途建模，分屏可共享正文而各组视图态独立；不同 Gateway／工作区、只读 revision 及 Diff 两端 SHALL 按原完整输入身份隔离。释放 SHALL 沿原引用计数、编辑器和 working-copy 生命周期，不按普通缓存淘汰 dirty 内容或仍被其它编辑器使用的模型。原刷新／watch 与异步 reload SHALL 保留其选项语义，不以远端缓存覆盖未保存编辑。

干净模型可按原规则释放，重启不要求恢复全部内存模型；有 working-copy 备份时沿原恢复，没有模型或备份时沿原读取与字节缓存校验。MUST NOT 新建所有干净正文的 IndexedDB 镜像，或把模型 version、stat ETag、保存 revision 与历史版本互相替代。

#### Scenario: 同文件分屏与关闭一组

- **WHEN** 同一文件在两个组同时 resolve 并编辑，随后关闭一个组
- **THEN** 复用原正文模型及在途加载，另一组的未保存正文保留，各组光标／滚动独立；最后引用释放按原 dirty／备份规则处理

#### Scenario: 同路径版本与后端变化

- **WHEN** A/B 同路径文本或同文件两个历史／Diff 版本同时打开，后端更新当前文件而一个模型仍有未保存内容
- **THEN** 各完整身份及版本保持独立，dirty 内容不被后台刷新覆盖，历史版本不被最新正文替换，保存仍执行原 CAS

### Requirement: 派生缓存必须绑定适用身份与代际

缓存及其异步结果 SHALL 按原层次绑定完整 owner、资源／查询／配置身份及适用版本，不使用标题、临时 route 或全局活动工作区作为身份。范围／认证／模型／配置／Provider 代际变化时 SHALL 废弃受影响结果，迟到写入不得复活旧代际。查询缓存、原 cursor、已加载导航页、目录镜像、历史镜像及领域索引 SHALL 保留既有用途、新鲜度和失效合同，不用一个全局版本或第二事实源代替。

原 ConfigurationCache SHALL 只缓存其真实非本地派生配置，已持久的本地设置不重复缓存；原 profile／容器 workspace 与后端业务工作区的归属 SHALL 明确区分。语言解析／tokenization／Worker 结果 SHALL 绑定实际模型及语言／配置版本；扩展派生缓存仅沿实际启用服务及原版本维护，不因 Electron 宿主自动切换 native 实现。缓存中的语言／搜索能力不得绕过发起前限定的业务 scope 或凭旧结果开放未启用能力。

#### Scenario: 配置更新与模型分析迟到

- **WHEN** 非本地配置更新后原缓存写入尚未完成，或模型／语言配置改变后旧 Worker 返回分析结果
- **THEN** 新代际不被旧结果覆盖，关闭／重开按原生命周期读取适用配置与分析，不能清所有存储掩盖竞态

#### Scenario: 历史镜像与文件字节的不同失效规则

- **WHEN** 后端暂时无法同步，而已存聊天历史镜像及普通文件字节缓存都存在
- **THEN** 历史按原合同可展示明确只读的旧镜像，普通文件不能把校验失败冒充当前读取成功；两者不混用权限、版本或执行能力

### Requirement: 缓存清理与升级不得删除受保护恢复数据

可再生缓存 SHALL 与原工作副本备份、本地文件历史、Composer 草稿、用户设置／快捷键、编辑器恢复数据、信任及凭据区别；扩展／语言包资源本体、安装记录及用户启用状态也不是可丢弃扫描缓存。清理／淘汰／升级 MUST NOT 删除这些受保护数据，不通过删除 profile、IndexedDB、全部 Storage 或 Electron clearStorageData 修复缓存问题。受保护格式不兼容或损坏 SHALL 保留原件并明确失败，不以空草稿／新设置冒充恢复成功。

清理入口 SHALL 复用原命令／设置与 owner 释放方法，明确类别及实际作用域；需要新增的 Action 不建立统一缓存平台或管理页面。缺少选择性清除 API 时 SHALL 使用原 reload／再验证或引擎淘汰，不新增原生 bridge 或虚构跨宿主清理能力。移除最近记录、关闭 Tab、删除业务对象、丢弃草稿及清缓存 SHALL 保持独立。临时查询取消或一个 picker 清缓存 MUST NOT 破坏独立编辑器／模型，实际业务写入仍经后端权威响应。

#### Scenario: 清理可再生缓存后重启

- **WHEN** 用户按明确类别清理查询或资源缓存，随后关闭并重启工作台
- **THEN** 对应缓存可按原来源重建，未保存备份、草稿、设置、历史记录及分屏恢复仍按原合同保留；原文件与会话未被删除，也不新增业务执行

#### Scenario: 不兼容版本不能靠清空恢复

- **WHEN** 应用升级使某个可再生条目版本不兼容，另有受保护恢复记录无法由新格式读取
- **THEN** 前者由原 owner 明确失效重建，后者保留并报错，不清整个 profile，也不将旧异步写入回灌到新代际

### Requirement: 运行资源与代码缓存必须遵守实际构建和宿主边界

发行入口、未内容寻址的 main／Worker SHALL 再验证，只有实际内容 hash 地址的静态资源才可长期 immutable；OAuth 回调保持 no-store。当前构建的入口、bundle 和 Worker SHALL 一致，升级不能静默使用旧文件代替新构建；开发环境保留 Vite/HMR，不新增全站离线 Service Worker 或缓存旧开发资源。

Web 与 Electron SHALL 共用 Workbench 构建和 browser 服务，Electron 仅使用既有固定 profile／partition／origin。Chromium 代码缓存由引擎管理，MUST NOT 为获取原生 CachedData 而引入原生 VS Code bootstrap、extension scanner 或本地 FS IPC，也不承诺不可观测的固定命中率。条件扩展／语言／Webview 服务 SHALL 在其真实宿主验收，未具备条件明确未完成。

#### Scenario: 无 hash 入口升级且旧缓存仍存在

- **WHEN** 旧版本静态缓存存在，新构建替换同名 main／Worker 并更新入口
- **THEN** 无 hash 资源重新验证并加载匹配版本，内容地址资源按真实地址复用；缺失或 Worker 不匹配明确失败，不加载旧 bundle 冒充新界面

#### Scenario: 开发热更新与桌面缓存

- **WHEN** Vite 修改源码触发热更新，或同一正式构建在 Web 与打包 Electron 中分别冷／热启动
- **THEN** 开发界面自动取得新代码，正式产物分别证明实际存储与功能；不要求导入原生缓存体系，也不以启动耗时或 Web parity 代替桌面行为证据

### Requirement: 非图片原字节复用保持原加载能力

允许预览的文本与其它文件字节 SHALL 消费原文件 owner 的私有条件读取；不同领域的正文与版本仍由各自 owner 管理，不把所有对象改走普通文件接口。原 Webview 的加载、权限与持久缓存由 resource-file-operations 的具名合同约束。

#### Scenario: 非图片资源复用同一字节链
- **WHEN** 用户重开允许读取的 UTF-8 文本，或原 Webview 加载其允许根中的 CSS／字体／脚本，资源未修改
- **THEN** 对应原读取能力和 MIME 保留，按原 HTTP／Webview 层校验后复用字节；不新建图片专用缓存、不把受保护 API 正文统一放入资源缓存，也不推进文本保存基线
