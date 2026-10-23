# 目录用途

存放模型 stream 测试的长期 provider cassette、scenario manifest 和独立业务 expectation。

## 资产职责树

```text
model_stream/
├── scenarios/<scenario_id>.jsonc       选择原始 cassette 和业务断言
├── handwritten/                        手工构造的 provider 原始 SSE frame
│   ├── openai_chat/                     OpenAI Chat Completions 协议输入
│   ├── openai_responses/                OpenAI Responses 协议输入
│   └── anthropic_messages/              Anthropic Messages 协议输入
└── expectations/<assertion_id>.json[c]  产品可见文本、工具调用等业务预期
```

选择链是 `scenario.asset → handwritten cassette` 和
`scenario.business_assertion → expectations`。Scenario 只连接输入资产与业务断言，不能
把 provider 原始 frame 改写成 SDK chunk 或产品事件；新增 provider 时沿协议增加 cassette
子目录，并继续通过 scenario 复用独立 expectation。

## 可修改内容

- `handwritten/` 中人为设计的 provider SSE frame。
- `scenarios/` 中资产选择关系，以及 `expectations/` 中业务断言引用。

## 不可修改内容

- 不写入 API key、Authorization、cookie 或未脱敏 prompt。
- 不把 SDK chunk、block 或业务事件快照写入 provider cassette。
- 不将 `out/tests/` 下的临时录制文件自动复制进来。

## 规范

- cassette 使用 `model_stream_cassette`，JSON 字段使用 `snake_case`。
- scenario id 和资产 id 使用 kebab-case；scenario 只选择 asset，不控制 transport。
- 修改资产后必须运行 asset loader 校验和对应 focused tests。
- 录制输出属于对应测试的 `out/tests/` 产物；只有经审查后才显式迁入长期 fixture。
- 模板示例；在整理 `AGENTS.md` 时请保留此行。
