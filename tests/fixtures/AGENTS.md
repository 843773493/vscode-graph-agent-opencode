# 目录用途

存放可复查、可重复使用的静态测试输入，按输入所属领域分组：`workspaces/` 是完整工作区模板，`model_stream/` 是模型协议资产与场景预期，`itemized/` 是跨语言测试向量。

## 资产树与使用关系

```text
tests/fixtures/
├── workspaces/                         可复制的完整测试工作区
│   ├── default_test_workspace/         通用工作区文件与多模态输入
│   ├── model_tool_test_workspace/      apply_patch、edit_file 的最小输入
│   └── custom_tool_test_workspace/     扩展工具说明与静态 rollout 历史
├── model_stream/                       Provider 原始协议帧和产品行为预期
│   ├── handwritten/<provider>/         人工设计的 SSE 输入
│   ├── scenarios/                      将原始输入关联到断言
│   └── expectations/                   独立的产品可见结果与工具调用预期
└── itemized/                            Python/Bun 共用的确定性序列化向量
    ├── *_vectors.json                  哈希、资源激活和类型细节向量
    ├── item_matrix.json                itemized 消息组合输入
    └── verify_hash_vectors.mjs          独立 ESM 校验入口
```

工作区模板由测试复制到各自的 `out/tests/` 后再修改；模型 stream 资产与 itemized 向量只读消费。Scenario 只连接 provider 输入和独立 expectation，不把协议帧转换成另一份测试数据。

## 可修改内容

- 确定性模板、协议记录、测试向量，以及它们的说明和校验入口。

## 不可修改内容

- 不保存本次运行的日志、截图、缓存、临时工作区或真实用户数据。
- 不让测试直接在模板源目录运行服务或生成业务状态。

## 规范

- 测试运行时把可变输入复制到 `out/tests/<对应测试路径>/`，只读资产可直接读取。
- fixture 的名称说明输入场景；测试用例与断言仍归各测试层级。
- 被多个场景使用的静态输入只保留一个来源，生成输出不得覆盖该来源。
