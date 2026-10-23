# 目录用途

`app/services/` 存放应用服务层代码，并按 `business`、`orchestration`、`infrastructure`、`mapping` 等子目录划分职责。

## 职责树

```text
services/
├── business/        业务规则与用户可见的业务能力
├── orchestration/   跨服务流程、执行顺序与生命周期编排
├── infrastructure/ 持久化、系统状态和外部能力
└── mapping/         DTO、事件与内部模型之间的纯转换
```

按问题定位目录：要改变“业务结果是什么”进入 `business/`；要改变“多个步骤怎样衔接”进入 `orchestration/`；要改变“状态如何读写或依赖什么系统能力”进入 `infrastructure/`；只改变“数据如何表达”进入 `mapping/`。一个功能跨越多层时，沿各层已有边界拆分职责，不把整条流程塞进单个目录。

# 可修改内容

- 可以在既有职责子目录下新增或调整服务实现。
- 可以新增服务子目录，但新子目录必须带自己的 `AGENTS.md`。
- 可以调整服务层依赖注入关系，前提是保持层次边界清晰。

# 不可修改内容

- 不把 API 路由、Agent runtime 细节或前端展示逻辑放在本层根目录。
- 不在父目录堆积具体服务实现；新服务应进入匹配职责的子目录。
- 不在服务层硬编码环境变量值。

# 规范

- business 放业务规则，orchestration 放流程编排，infrastructure 放系统/外部能力，mapping 放模型转换。
- 服务失败时直接抛出明确错误，不要静默失败。
- 修改服务层后应运行相关 Python 静态检查或编译检查，并尽量补充对应单测。
