## ADDED Requirements

### Requirement: 目录与文件描述提供真实轻量观察值
原资源导航 owner SHALL 为本机实际文件与目录的 describe 结果提供真实节点类型、字节长度及毫秒时间戳；同一观察值可随 list/search 节点投影返回，不创建逐文件持久身份。describe MUST NOT 读取文件正文、计算全文 hash 或推进编辑器的内容 revision。无文件系统观察值的领域资源可以不提供 stat，不得用固定成功或虚假时间冒充实际文件。资源缺失、权限不足和目标归属错误继续使用原 owner 的错误合同。

工作台唯一文件内部投影 SHALL 保存稳定 Gateway／工作区归属、后端公布的规范根及相对子路径，并支持原 URI joinPath、relativePath 和往返定位。临时 route 不进入编辑器身份或恢复记录，执行前重新解析已登记目标；旧记录缺少可靠归属时明确不可恢复，不改投当前工作区。目录与正文读写 SHALL 消费同一原寻址／文件 owner；保存继续使用原内容 revision／CAS，轻量 stat 不代替写入基线。

#### Scenario: 从规范根拼接中文与字面百分号文件
- **WHEN** 原版编辑器从后端规范目录根拼接多层中文、空格、emoji 和字面百分号文件名
- **THEN** 内部投影可取得原相对路径，往返回到同一规范资源 URI，不丢失编码或混入其它根的同名文件

#### Scenario: 描述目录不读取正文
- **WHEN** 工作台在打开文本前 stat 真实目录根或非文本文件
- **THEN** 描述返回真实类型及文件系统观察值，不调用文本读取；不存在的目录明确失败，内容模型的写入基线不受 stat 影响

#### Scenario: 已登记 route 改变后保存
- **WHEN** 同一稳定 Gateway／工作区的 route 改变，或编辑器 stat 后后台文件正文改变
- **THEN** 编辑器身份保持不变、请求重解原 owner；保存仍携带实际读入正文的原 revision，不能借新 stat 接受覆盖冲突
