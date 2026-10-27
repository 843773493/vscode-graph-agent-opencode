# 目录用途

持久任务的受管理 worker、恢复和触发至 source 动作的流程编排。

# 可修改内容

运行时初始化、逐任务有界推进、停止准入、恢复对账和显式失败报告。

`conditions.py`按冻结定义与触发事实执行always/rule/script判断；规则使用标准JSON Pointer与JSON值相等，不读取当前外部状态；脚本通过共用平台监督器在所属工作区执行，不持任务管理锁。返回后重验revision；原生标准错误只进入原始诊断日志，判断结果与动作身份由原账本保存。checking的未知脚本结果禁止自动重跑，纯规则可从原冻结输入重新计算。

# 不可修改内容

不另建任务账本、canonical 正文、执行器或 Goal 独立恢复入口。

# 规范

start 只等待 worker 初始化；慢任务不得阻塞其他已恢复任务。所有后台任务归 worker 持有并在关闭时排空，发布成功与执行接受分开表达。

`contexts.py`只从冻结定义、触发及判断构造动作输入。event保留逐次运行身份；state以任务为稳定来源，内容revision不包含run_id、触发键和判断理由，状态素材仍是原trigger payload及condition result。正文归原canonical owner，禁止任务另存上下文历史。

执行结束事件只唤醒同一worker；触发事实来自原canonical终态提交，按原owner有界读取并与来源游标原子接受，不建立第二份执行历史。新订阅/更换来源和暂停后恢复取当前明确起点，改名保留游标。

模型判断沿同一source/wakeup及执行准入，显式绑定独立Thread，使用该Thread自己的Agent/工具配置，不继承工作Thread能力。运行只保存原准入引用；未知结果等待终态唤醒并释放worker槽，恢复先对账原身份。只读取本次execution最后一次ModelCall的canonical输出，并校验run_id/definition_revision；迟到结果仍由原决定事务抑制动作。

`goal.py`只处理一个 Goal 应用工作项和暂停前用时结算，不持有 worker、恢复循环、事件订阅或关闭生命周期；Goal 与其他任务共用 runtime 的有界槽位及动作准入。

Goal与普通组件任务的恢复身份在初始化时登记，恢复工作仍在初始化回执后推进；Goal冻结原身份/revision，进度仅为内存展示态。成功结束恢复和原Goal修改/清除均使同一管理快照失效，不能另建应用调度器或持久镜像。
