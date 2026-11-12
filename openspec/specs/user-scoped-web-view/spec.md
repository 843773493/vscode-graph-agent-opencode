# user-scoped-web-view Specification

## Purpose
浏览器展示状态由现有本地存储与 Workbench profile 持有，按 Gateway、工作区、Session、Thread 及窗口或编辑器归属隔离，不依赖已退役的 Gateway 用户体系。

## Requirements
### Requirement: 浏览器视图隔离

前端 SHALL 复用既有 IndexedDB 和 Workbench 存储保存主题、布局、Composer 草稿及阅读状态；工作区与会话状态必须使用完整稳定 owner。首次使用 SHALL 采用产品默认值，不读取或自动导入旧用户档案。

#### Scenario: 切换工作区或 Thread

- **WHEN** 用户切换不同 Gateway、工作区或 Thread
- **THEN** 视图和草稿按对应归属恢复，不混入其它 owner 的状态

#### Scenario: 新浏览器访问

- **WHEN** 使用没有该站点本地数据的浏览器访问同一 Gateway
- **THEN** 使用默认展示状态，不从 Gateway 用户视图恢复另一浏览器的偏好

### Requirement: 会话阅读位置恢复

前端 SHALL 保存历史阅读锚点、偏移、是否跟随最新消息和投影版本；恢复优先使用稳定 Turn 锚点，不能仅依赖像素值。

#### Scenario: 返回已浏览 Thread

- **WHEN** 用户返回同一浏览器已浏览的 Thread
- **THEN** 恢复对应阅读位置和模式，不默认跳到尾部

#### Scenario: 锚点已不可恢复

- **WHEN** 原锚点因投影或历史变化不可用
- **THEN** 明确报告恢复结果并使用最近可用位置或尾部，不伪造原位置

### Requirement: 新消息与阅读位置共存

用户阅读旧历史时 SHALL 保持其位置并展示新消息提示；跟随最新消息时继续跟随输出。

#### Scenario: 阅读旧历史时收到新输出

- **WHEN** 新消息到达且用户未跟随尾部
- **THEN** 保留当前位置和阅读模式

#### Scenario: 跟随尾部收到新输出

- **WHEN** 用户处于跟随最新消息模式
- **THEN** 更新内容并继续跟随尾部
