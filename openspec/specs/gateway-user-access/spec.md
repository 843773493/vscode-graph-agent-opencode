# gateway-user-access Specification

## Purpose
浏览器和本机自动化复用 Gateway 既有本机凭据；联邦认证保持独立，不再维护普通用户、游客或访问占用体系。
## Requirements
### Requirement: 本机访问使用既有凭据

浏览器 SHALL 从既有同站点凭据入口取得本机 token，直接访问受保护业务 API。Gateway MUST NOT 创建普通用户、游客、访问租约、接管状态或访问心跳，也不提供旧用户生命周期及视图 API。联邦凭据、目标归属及各业务权限检查 SHALL 保留。

#### Scenario: 新浏览器首次打开

- **WHEN** 浏览器首次打开工作台
- **THEN** 页面取得本机凭据后直接初始化工作区，不请求用户选择、游客创建或心跳入口

#### Scenario: 无效凭据

- **WHEN** 业务请求携带无效或缺失凭据
- **THEN** Gateway 明确拒绝；前端只对明确过期的本机 token 执行一次有界刷新，不创建游客或无限重试

### Requirement: 同源原生资源复用本机凭据

凭据入口 SHALL 同时设置使用现有本机 token 的 HttpOnly、SameSite Strict Cookie，路径限定为 `/api/gateway`，HTTPS 下启用 Secure。图片与扩展包字节入口 SHALL 仅在没有显式凭据且请求为同源时接受该 Cookie；其它控制与业务 API 继续要求显式凭据。Cookie MUST NOT 建立第二套身份、访问租约或持久账本，token 不进入资源 URL。

#### Scenario: 原生图片或模块读取

- **WHEN** 已初始化的同源页面通过原生资源 URL 读取图片或扩展包
- **THEN** 入口校验既有本机凭据和原目标权限后返回正文，不依赖游客身份

#### Scenario: 跨源或显式错误凭据

- **WHEN** 跨源请求试图使用 Cookie，或请求显式提供错误凭据
- **THEN** 拒绝访问，不回落到 Cookie；有效联邦认证继续沿原授权边界执行

### Requirement: 保留退役体系的历史数据

退出旧访问体系 MUST NOT 自动删除或重分配既有用户目录、SQLite 历史行及浏览器旧账户数据。当前链路 MUST NOT 读取旧账户作为本机身份或自动重放旧账户的待办操作；已支持的离线迁移保留其原有数据保护职责。

#### Scenario: 旧账户数据存在

- **WHEN** Gateway 或浏览器以本机凭据启动
- **THEN** 旧数据保留，当前操作使用明确的本机分区，不猜测历史记录的归属

### Requirement: 本机自动化搜索复用同一联邦处理链

Gateway SHALL 为本机 Agent 与 Python 脚本提供具名只读入口 `/api/gateway/search/agent`，使用既有本机私有凭据认证，不依赖浏览器、用户视图或访问租约，不自动创建游客或接管用户。该入口与浏览器搜索 SHALL 使用相同请求、事件、范围解析、Provider、调度、预算及输出编码实现，不建立第二套搜索或跨目标队列。普通浏览器 `/api/gateway/search` SHALL 使用同一既有本机或联邦凭据认证，不要求用户访问租约；本机入口的凭据授权 MUST NOT 被扩大为豁免其它工作区 API 的访问校验。

#### Scenario: 关闭浏览器后执行工具搜索

- **WHEN** 已授权后台 Agent 或持有本机私有凭据的脚本在没有浏览器 Cookie 时查询明确工作区集合
- **THEN** 通过具名本机入口返回同一 typed 搜索流及独立续点，只查询选择的目标，不创建用户访问会话

#### Scenario: 普通浏览器与本机入口校验既有凭据

- **WHEN** 普通搜索或本机入口缺少有效访问凭据
- **THEN** 明确拒绝，不创建游客或访问租约，原目标和联邦权限检查保持
