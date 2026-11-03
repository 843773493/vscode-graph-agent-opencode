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

### Requirement: 工作区消息查询由后端限定发现与正文预算

工作区范围的消息搜索 SHALL 由原历史服务编排 catalog 与 SessionThread owner 的有界读取，发现 main 与已发布 child，再复用同一 canonical 正文查询。浏览器不得遍历全量 Session 或以已打开标签替代发现。多个 Thread SHALL 共用本页正文条数、字节量和执行期限；目录、Thread 数和结果数分别受限。返回命中 SHALL 保留精确 Thread 的归属、名称与原消息引用，不创建跨 owner 全局正文索引。

续页 SHALL 固定 Gateway、workspace、查询、catalog revision、当前 Session 的 Thread 目录版本和当前历史位置。各 Thread 是其实际读取时的快照，不承诺跨库同一时刻；已完成分支新增消息需重新查询。目录或当前历史失效 SHALL 返回冲突，不能丢弃旧 cursor 后悄悄重查。

#### Scenario: 未打开 child 的工作区查询

- **WHEN** 用户在所选工作区查询只有未打开 child 才有的正文
- **THEN** 后端发现该 child 并返回精确归属，尚无历史的 child 不被物化，其他工作区不被读取

#### Scenario: 跨 Thread 预算与失效

- **WHEN** 多个 Thread 的累计正文读取达到本页预算，或续查期间目录／当前历史发生变化
- **THEN** 前者返回明确受限状态及原位置续查，后者明确拒绝旧页；均不得伪报完整无匹配

### Requirement: 搜索命中通过原历史读取入口核验

原 around 历史入口 SHALL 接受独立的消息位置引用，在同一读快照核验 item 仍属于指定 Turn、可见视图、正文版本及位置范围，再沿唯一历史分页链加载。该引用不得用于更换 Thread 或推导其它机器路径。

核验成功时，around 响应 SHALL 附带本次定位的一条 canonical 原文及身份、位置引用。消费者在所属 Turn 内临时展示原文并高亮码点区间，等待虚拟列表实际挂载后定位；不得从截断片段或 Markdown DOM 猜测位置。该原文只属于本次导航，不写入时间线缓存或持久展示态，关闭定位即释放。

#### Scenario: 搜索后视图或正文引用失效

- **WHEN** 用户接受的消息已不在目标历史视图、版本不符或位置越界
- **THEN** 返回明确的历史引用冲突，不以当前最新消息、main Thread 或会话顶部替代

### Requirement: 工具结果正文沿同一历史 owner 显式查询

精确 Thread 与工作区历史搜索 SHALL 接受显式 `tool_result` 类别；省略类别仍只搜索用户／助手消息，两者 cursor 不得混用。工具结果 SHALL 从当前可见 canonical item 及其原完整输出引用读取，沿消息搜索同一条链路消费共享条数、字节和期限预算，不查询 Trace 或重放工具。完整输出超出剩余预算 SHALL 明确受限；单项超出硬上限不得返回不能推进的 cursor。

同 Turn、同 ModelCall 的 stream 与 checkpoint 工具结果载体 SHALL 沿原历史详情归并顺序，取 `item_sequence` 最早的可见载体，不固定优先某种载体。保留 canonical item 与 ModelCall 限定调用身份，不按同名工具或裸调用 ID 合并跨执行结果。命中正文位置采用相同 Unicode 码点合同，完整输出的既有版本核对只在同次读取中完成，不新增文件预读。

原 around 入口 SHALL 在同一快照核验工具引用，定点加载原工具详情，临时原文展示于精确结果行。完整输出已删除、改写或位置失效 SHALL 返回历史引用冲突，不展示另一版本，不回退到截断片段或同名结果。

#### Scenario: 完整工具输出中间的命中

- **WHEN** 未打开 child 的工具结果只在已物化完整输出的中间包含查询词
- **THEN** 显式工具类别返回精确 canonical 结果，原详情加载后显示并定位完整原文，消息类别不混入该结果，查询不产生模型或工具执行

#### Scenario: 完整输出失效

- **WHEN** 搜索后原完整输出被删除或被改为同长度的不同内容
- **THEN** 原 around 入口明确拒绝旧引用，保留其他可用历史，不把新正文当作原命中
