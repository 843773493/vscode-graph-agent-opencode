## ADDED Requirements

### Requirement: 消息正文查询复用 canonical 历史

精确 SessionThread 的消息搜索 SHALL 从当前历史视图可见的 canonical 用户输入和助手输出读取正文，不查询 Trace、前端缓存或另一份完整消息历史。首版 SHALL 明确采用区分大小写的字面量、非重叠匹配；不混入 reasoning、工具结果、受保护正文或内部执行。每页 SHALL 限制扫描条数、读取字节、结果数和执行期限，分别返回覆盖、受限原因、扫描量及 owner cursor；未搜索完成不得伪报完整零匹配。

命中 SHALL 保留 Session、Thread、Turn、canonical item、消息身份和不可变内容版本，位置采用从零开始、右端不包含的 Unicode 码点区间，片段携带自身起点且仅供展示。分页 SHALL 绑定精确 owner、查询、rollout、历史视图及其版本；原视图失效返回明确冲突，不能混合旧页。

#### Scenario: 未加载的 child 消息命中

- **WHEN** 用户查询精确 child 中尚未打开或加载的历史正文
- **THEN** 原 owner 返回该 child 的 canonical 命中及精确 Turn，不读取 main 或依赖浏览器缓存

#### Scenario: 限制与续查

- **WHEN** 查询达到结果、扫描、字节或时间限制
- **THEN** 返回明确受限状态及适用续查，不能以没有匹配表示完整搜索结束；单项超过硬字节上限时明确不可继续该页，不反复返回无法推进的 cursor

### Requirement: 搜索命中通过原历史读取入口核验

原 around 历史入口 SHALL 接受独立的消息位置引用，在同一读快照核验 item 仍属于指定 Turn、可见视图、正文版本及位置范围，再沿唯一历史分页链加载。该引用不得用于更换 Thread 或推导其它机器路径。

#### Scenario: 搜索后视图或正文引用失效

- **WHEN** 用户接受的消息已不在目标历史视图、版本不符或位置越界
- **THEN** 返回明确的历史引用冲突，不以当前最新消息、main Thread 或会话顶部替代
