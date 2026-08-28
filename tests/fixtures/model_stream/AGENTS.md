# 目录用途

存放模型 stream 测试的长期上游资产、scenario manifest 和独立业务 expectation。

## 可修改内容

- `handwritten/` 中人为设计的 provider SSE frame。
- `recorded/` 中经过脱敏和审查的上游 cassette。
- `scenarios/` 中资产选择关系，以及 `expectations/` 中业务断言引用。

## 不可修改内容

- 不写入 API key、Authorization、cookie 或未脱敏 prompt。
- 不把 SDK chunk、block 或业务事件快照写入 provider cassette。
- 不将 `out/tests/` 下的临时录制文件自动复制进来。

## 规范

- cassette 使用 `model_stream_cassette`，JSON 字段使用 `snake_case`。
- scenario id 和资产 id 使用 kebab-case；scenario 只选择 asset，不控制 transport。
- 修改资产后必须运行 asset loader 校验和对应 focused tests。
