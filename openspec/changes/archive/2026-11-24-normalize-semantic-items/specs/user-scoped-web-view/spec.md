## MODIFIED Requirements

### Requirement: 浏览器视图隔离

前端 SHALL 复用既有 IndexedDB 和 Workbench 存储保存主题、布局、Composer 草稿、折叠和阅读状态，工作区与会话状态使用完整稳定 owner。阅读与折叠状态 MUST 属于当前客户端安装／浏览器配置；相同 Gateway、工作区、Session 和 Thread 的业务身份可以共享，不意味着 Web、Electron、移动端或其它浏览器共享阅读游标。首次使用采用产品默认值，不从后端导入其它客户端展示状态。

#### Scenario: 切换工作区或 Thread
- **WHEN** 用户在同一客户端切换 Gateway、工作区或 Thread
- **THEN** 按各自完整 scope 恢复视图和草稿，不混入其它 owner 数据

#### Scenario: 两个客户端独立阅读
- **WHEN** 浏览器 A 阅读同一 Thread 的 Item X，另一个独立客户端阅读 Item Y
- **THEN** A 的滚动、折叠、刷新和重新打开不改变另一客户端的位置；后端更新只同步业务内容，不下发 A 的阅读状态

#### Scenario: 新浏览器访问
- **WHEN** 没有该站点本地数据的浏览器访问相同 Gateway
- **THEN** 使用默认展示状态，不从 Gateway 用户视图或另一客户端恢复阅读偏好

#### Scenario: main 别名解析与 fork
- **WHEN** 用户以 main 入口打开或 fork 后进入新 Thread
- **THEN** main 先解析为权威真实 Thread ID，再按完整 scope 读写本客户端视图；fork 使用新 owner，不继承来源阅读位置或跨客户端偏好。

### Requirement: 会话阅读位置恢复

前端 SHALL 保存稳定来源＋展示 slot、客户端内内容位置、相对视口偏移、follow_latest、折叠选择和阅读格式版本。已提交内容的来源 MUST 只引用真实 `item_id`，不包含 `part_id`、Part ordinal、DOM 下标或展示父容器；标题与上下文标记继续引用原过程／组首 Item／消费边界。长正文的块号与局部偏移仅为当前渲染器的内容内位置，不升级为后端实体、URI 或业务操作边界。

恢复 SHALL 先应用折叠状态，再沿原 owner 有界加载 Item 及其可见祖先；普通恢复不强制展开，显式 Item 跳转遵循原展开规则。宽度、折叠和分页改变时保持相同内容附近，不依赖绝对 scrollTop 或整轮像素偏移。follow_latest 与锚点保位互斥，用户主动滚动或取消后停止旧定位补偿。

未提交 draft SHALL 只在当前编辑器临时定位，持久记录使用可恢复的真实过程来源和跟随模式；实际提交后按来源关联替换，不伪造持久 Item。支持来源的旧阅读记录只有能确证 Item 对应关系时才能转换；未支持格式保留原记录并明确恢复受限，不删除全库或静默丢弃 Part 坐标后宣称精确恢复。

#### Scenario: 返回已浏览 Thread
- **WHEN** 用户返回同一客户端已浏览的 Thread
- **THEN** 恢复该 scope 的 Item、折叠和跟随模式，不默认跳尾；单个长文本内使用本地内容位置恢复附近内容

#### Scenario: 目标位于折叠组内
- **WHEN** 所读 Item 被过程或活动组收起
- **THEN** 定位最近真实可见祖先，保留目标 Item 身份，不自动展开或误选同名兄弟内容

#### Scenario: 宽度或内容高度变化
- **WHEN** 上方插入上下文虚线、final 内容移出过程区、图片增高或宽度重排
- **THEN** 保持原 Item／slot 附近阅读位置，不重新选视口外目标；final 容器变更不改变内容 Item 节点键

#### Scenario: 虚拟列表尚未挂载
- **WHEN** Item 因分页或虚拟列表尚未成为 DOM 节点
- **THEN** 沿原分页／虚拟列表入口有界加载并挂载，不能判为已删除、全量拉历史或定时无限重试

#### Scenario: 锚点已不可恢复
- **WHEN** 原 owner 确认 Item 不可见、已删除、无权限、过期，或请求遭遇网络／服务故障
- **THEN** 区分真实结果；普通恢复明确说明受限并选择声明的可用位置，显式跳转报告目标结果，网络错误不能伪装成目标删除或空成功

#### Scenario: 流式 Part 尚未持久提交
- **WHEN** 当前阅读来源仍为未提交的流式 draft
- **THEN** 仅在编辑器内临时定位，持久记录使用可恢复过程来源；真实提交沿明确来源关联接续，不保存 Part 或伪造 Item ID。
