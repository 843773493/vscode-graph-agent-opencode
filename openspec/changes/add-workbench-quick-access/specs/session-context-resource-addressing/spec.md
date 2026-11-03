## ADDED Requirements

### Requirement: 联邦上下文查询复用原来源与分页

工作区搜索 SHALL 提供独立 `context/content` Provider，委托原上下文查询 owner，保留 `effective_context`、`session_catalog`、`session_information` 来源、literal／regex、大小写、字符预算及原 revision／cursor。省略来源仍为有效上下文。该类别 MUST NOT 用 canonical 消息正文、文件或日志搜索替代；精确单资源读取及 inventory 继续沿原接口。

联邦请求 SHALL 将来源和字符预算作为 typed 领域选项传递，由明确 owner 生成工作区上下文资源身份和 URI，不接受调用方把另一工作区资源塞入该分支。命中 SHALL 保留原资源身份、规范 URI、来源、record_index、码点区间和独立版本；分支保留原页续点及受限说明，不合成跨库版本或全局正文偏移。Gateway 仍只处理范围与调度，不读取上下文存储。

#### Scenario: 无浏览器搜索有效上下文

- **WHEN** 工具或脚本在明确工作区集合查询产品执行产生的有效上下文
- **THEN** 同一 Gateway 调度返回各 owner 的原上下文页，可沿原资源读取入口按版本精确读取，未选工作区不参与查询

#### Scenario: 上下文续页与来源变化

- **WHEN** 沿原范围续查，或把旧 cursor 用于不同来源／查询／版本
- **THEN** 前者保留原 owner 的分页结果，后者按原合同明确拒绝，不悄悄重查或换成普通消息搜索
