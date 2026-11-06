## ADDED Requirements

### Requirement: Webview 资源缓存沿原版加载与隔离规则

受控 Webview 文件资源 SHALL 沿原 asWebviewUri、Service Worker Cache Storage、loadLocalResource 和同一 FileService 加载；有缓存时携带其 ETag 校验，NotModified 复用缓存，新内容沿原流显示与更新。普通图片编辑器 SHALL 保留原 FileService／Blob 路径，不被替换成 Webview 来伪造缓存接入。两条原加载链 MUST NOT 再叠加应用自管字节库、home 文件镜像、原生文件系统 IPC 或第二资源解析器。外部 HTTP(S) 图片遵守其来源和原 Webview 安全／HTTP 策略，不承诺全部第三方内容可缓存。

缓存使用 SHALL 保留稳定 Gateway／工作区／规范资源归属、原 Webview localResourceRoots、origin／profile 与当前认证边界。存在缓存 MUST NOT 跳过当前权限校验，退出或身份切换 SHALL 取消旧读取并撤销旧展示引用；新上下文验证前不使用旧资源。原 watcher 的变更、删除、重连及 overflow SHALL 触发原文件服务重新核实；目标删除、不可达或拒绝访问 SHALL 明确失败，不能默认以缓存冒充当前成功。缓存键、业务 URI 和日志不得嵌凭据；路由重用不能改变原资源归属。

Range SHALL 保留原 206、Content-Range、取消和按需字节读取，不进入完整资源缓存，不为预览或缓存而完整下载音视频。仅有修改时间／大小时 SHALL 明确其观察值局限，不承诺检测二者均未变化的任意外部改写或据此提供可复现版本。

#### Scenario: 同路径目标、认证及 route 变化
- **WHEN** A/B 有同显示路径图片，客户端切换身份，或旧 route 被另一个工作区复用
- **THEN** 当前授权及完整资源身份决定校验和读取，A 缓存不成为 B 或新身份的成功响应；迟到旧请求不覆盖当前图片

#### Scenario: 缓存存在但原资源不可用
- **WHEN** 图片已有缓存，但文件被删除、权限被撤销或原目标断连
- **THEN** 当前读取明确失败，缓存不绕过原 localResourceRoots 或权限，也不作为离线成功回退

#### Scenario: Webview 与分段媒体各用原链路
- **WHEN** Webview 对未变文件请求完整 GET，另一媒体请求指定 Range
- **THEN** 前者经宿主校验后可复用 Cache Storage，后者只读取对应范围且不写成完整缓存；两者不新增解析／下载服务或推进保存基线

### Requirement: 资源缓存跨 Web 与 Electron 持久且可丢弃

Web SHALL 使用浏览器 origin 的原 HTTP 缓存／Cache Storage；Electron SHALL 使用同一前端读取链、稳定 renderer origin、既有 persist partition 和固定 profile，由 Chromium 管理磁盘格式，正常重启不主动清资源缓存。两者 MUST NOT 为此分叉前端源码或添加 native FS IPC。缓存沿引擎配额、淘汰与原缓存版本生命周期，MUST NOT 无界预热或成为业务权威。资源缓存清理 SHALL 只作用于所属缓存，不删除聊天历史、设置、草稿或后端文件。淘汰按原权限重新读取，来源读取结果仍按原合同表达，不能以成功读取宣称持久化成功。

官方隔离 Webview Worker 的 Cache Storage 异步写入 SHALL 沿用上游尽力而为语义：配额或写入失败不影响已成功取得资源的响应，不要求额外日志或宿主错误上报。MUST NOT 为补充该诊断复制 Worker、另行部署隔离宿主或增加写入探测。资源读取失败、权限拒绝及其它缓存层的原诊断要求继续适用。

验收 SHALL 在真实 Gateway 和目标工作区下分别覆盖 Web 与打包 Electron 的普通图片及 Webview：首次、再次、正常重启、修改更新、身份隔离、断连／删除、Range 及缓存淘汰。证据 SHALL 区分网络 304 与浏览器合成的 fetch 200，记录实际条件请求、服务端状态及正文传输量，不以图片可见、进程启动、上游源码存在或浏览器 parity 代替原生证明。原 Webview Worker／宿主未匹配固定版本时 SHALL 明确未完成，不替换原编辑器绕开。

#### Scenario: Electron 重启后复用资源字节
- **WHEN** 打包 Electron 已缓存普通图片和 Webview 资源，正常退出后以相同 profile／来源重启，缓存未被引擎淘汰且原资源未变
- **THEN** 两类入口均经原链路校验后复用字节而不重传正文；证据包含实际产物、缓存存储边界和网络行为，不依赖开发服务器或源码目录

#### Scenario: 缓存被淘汰
- **WHEN** 浏览器淘汰缓存，而原资源仍可访问
- **THEN** 原读取链可重新取得正确字节；不创建 home 文件副本或清除其它展示态补偿

#### Scenario: 官方 Webview 缓存写入失败
- **WHEN** 官方隔离 Worker 已取得正确资源，但异步缓存写入因配额或其它写入异常失败
- **THEN** 沿用上游响应与错误处理，不要求新增写入失败诊断，不宣称资源已经持久化；后续读取继续遵守原权限和校验合同
