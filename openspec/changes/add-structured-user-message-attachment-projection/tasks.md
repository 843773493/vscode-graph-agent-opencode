## 1. Canonical 用户 block 与附件数据契约

- [x] 1.1 定义用户消息 block walker 的输入、文本 block 识别、附件 manifest 识别、未知 block 保留和 display/provider/full 三种输出契约，覆盖字符串、混合列表与未知 provider block。
- [x] 1.2 扩展附件引用与会话附件存储的通用元数据和相对路径解析，确保稳定 `file_id`、文件名、MIME 类型、原件/预览变体及错误状态可独立读取。
- [x] 1.3 将图片预览生成规则统一为最长边 `min(512, max(原始宽度, 原始高度))`，增加不放大、重复生成、原件保留和预览失败状态的后端测试。
- [x] 1.4 检查附件清理、会话范围校验和路径解析，确保不暴露绝对路径、不扫描磁盘重建身份，并对原件保存/路径解析错误快速返回详细诊断。

## 2. 用户消息构造与 canonical 持久化

- [x] 2.1 将现有用户附件组装流程收敛为唯一的 `UserContentBuilder` 入口，一次性生成有序 `HumanMessage.content` block 序列、manifest 文本、preview block、通用 block metadata 和附件索引；复用现有标记的转义规则，但不调用 `InternalMessageFactory`。
- [x] 2.2 为有可用 preview 的图片构造模型可用的 `image_url` preview block，确保 block 使用预览而非原图，并在无预览时保留 manifest 文本和明确状态。
- [x] 2.3 调整 checkpoint、rollout 和消息 metadata 的写入边界，保留模型实际使用的用户 block 顺序及 preview data URL/base64；禁止将 preview 正文复制进 SQLite 索引。
- [x] 2.4 保证 `InternalMessageFactory` 与 `UserContentBuilder` 保持并列且语义隔离；provider 投影和 Web display 投影均创建新对象，不就地修改 `HumanMessage`、附件引用、checkpoint、rollout JSONL 或 SQLite 行。

## 3. LiteLLM 与 provider 请求投影

- [x] 3.1 实现用户 content 的不可变 provider projection，按目标 provider 能力分别决定 text、manifest 和 rich block 的发送结果，并输出可诊断的 `not_sent`/`projection_failed` 信息。
- [x] 3.2 接入 LiteLLM 可统一的 Chat Completions 内容转换，并补齐 Responses 的 `input_text`/`input_image` 与 Anthropic Messages 对应用户 block 的最小目标适配。
- [x] 3.3 将用户图片 preview 的能力判断接入现有 capability routing；对不支持 rich block 的 provider 保留相对路径文本，不新增附件工具或应用级 PDF modality。
- [x] 3.4 添加跨 provider 的请求快照与 source immutability 测试，验证支持图像输入、不支持图像输入、通用文件和未知 block 的投影差异及重复读取确定性。

## 4. Rollout 与历史 display projection

- [x] 4.1 修改 `RolloutHistoryReader` 的用户消息读取，按 canonical block 提取可见文本，禁止对结构化用户 content 使用整体 `json.dumps`。
- [x] 4.2 从用户消息 block metadata 与 `response_metadata.attachments` 生成独立附件 DTO，确保默认历史 `content` 不含 `image_url`、data URL、provider block 或完整 content 数组；新消息不依赖重复的 `display_content`。
- [x] 4.3 为用户 content block 坐标、附件身份和有界 projection 补齐 rollout/SQLite 读写与 checkpoint roundtrip，验证 full 恢复仍保留 preview base64。
- [x] 4.4 保持 projection/detail/full 读取边界：默认历史只读取文本与附件摘要，完整模型恢复才读取 canonical content，附件原件由稳定 file id 按需定位。
- [x] 4.5 验证损坏 block、缺失附件变体、非法 file id 和超出历史预算时均返回明确错误或可识别的截断/失败状态，不返回空白成功值。

## 5. Web 用户消息与会话资源展示

- [x] 5.1 调整当前 Web 用户消息数据流，使正文 renderer 只接收 display text，附件区域只接收稳定附件 DTO，永不从 raw content 或正文文本反解析附件身份。
- [x] 5.2 让附件卡片优先加载 thumbnail，并在缩略图缺失、通用文件或读取失败时显示对应元数据和错误状态；原件只在用户明确查看时请求。
- [x] 5.3 将附件点击动作接入主窗口的右侧侧边栏会话资源入口，使用 `file_id` 打开原图/原件，保持资源归属当前会话而不是 Gateway 或工作区面板。
- [x] 5.4 调整 Composer 的附件提交，使通用 MIME 文件可以进入统一持久化流程；图片仍生成 preview，非图片不被伪造为图片 block，并保留用户可见的上传失败反馈。

## 6. 验证、迁移与交付

- [x] 6.1 增加 block walker、附件变体、历史 user projection、provider projection 和错误诊断的单元测试，覆盖中文文本、混合 block、未知 block 与异常输入。
- [x] 6.2 使用 `out/tests/<对应测试路径>/workspace/` 或 `out/tests/temp/<task_name>/workspace/` 的隔离工作区增加 checkpoint/rollout/历史 API 集成测试，禁止修改 fixture 源目录。
- [x] 6.3 增加确定性 Web 组件/API 测试，验证用户正文不出现 `image_url` JSON/base64、缩略图渐进加载、附件失败态和右侧资源跳转。
- [x] 6.4 对变更涉及的 Python 模块运行静态分析和 pytest；修改 `src/clients/web` 后运行 `bun run --cwd src/clients/web build`，并通过 8011 经 Gateway 验证真实 API 链路。
- [x] 6.5 完成 canonical checkpoint/rollout 不重写检查、OpenSpec 验证和变更产物清单，确认无 reference_repo 测试被执行、无临时二进制写入项目根目录。
