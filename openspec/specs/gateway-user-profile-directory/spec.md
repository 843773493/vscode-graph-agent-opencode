# gateway-user-profile-directory Specification

## Purpose
明确旧 Gateway 用户档案退出后的数据边界，保留历史原件，避免重新引入按账户维护的配置链路。

## Requirements
### Requirement: 退出用户档案运行期管理

Gateway MUST NOT 为当前浏览器访问创建、选择、修改或删除旧用户 profile。既有用户目录及历史数据库内容 SHALL 保留，不自动迁入本机默认账户，也不提供长期双读或双写链路。

#### Scenario: 旧 profile 已存在

- **WHEN** 以本机凭据启动或修改当前展示偏好
- **THEN** 旧 profile 原件保持不变，当前展示状态由浏览器既有存储管理

### Requirement: 当前图片与配置归属明确

当前主题图片 SHALL 使用 Gateway 原有图片资源 owner；主题、布局与阅读偏好 SHALL 使用浏览器既有展示态存储。连接、凭据与 Gateway 控制面数据继续由各自原 owner 管理，不写入旧用户目录。

#### Scenario: 上传图片并保存偏好

- **WHEN** 浏览器上传背景图片并保存展示设置
- **THEN** 图片经受保护入口存入 Gateway 资源范围，偏好写入浏览器存储，不创建用户目录

### Requirement: 不新增用户配置同步

系统 MUST NOT 因清理旧用户体系而初始化 Git 同步、自动推送、或猜测旧用户数据归属。

#### Scenario: 旧数据尚未人工整理

- **WHEN** 用户尚未决定历史 profile 的后续处理
- **THEN** 系统保留原件且不自动导入或删除
