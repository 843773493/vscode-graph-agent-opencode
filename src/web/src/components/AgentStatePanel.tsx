import { formatTime } from "../utils/format";

type JsonRecord = Record<string, unknown>;

interface ParsedLine {
  line: number;
  record: JsonRecord;
}

function isRecord(value: unknown): value is JsonRecord {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function asString(value: unknown): string {
  return typeof value === "string" ? value : "";
}

function prettyJson(value: unknown): string {
  if (typeof value === "string") {
    const trimmed = value.trim();
    if (!trimmed) {
      return "";
    }
    try {
      return JSON.stringify(JSON.parse(trimmed), null, 2);
    } catch {
      return value;
    }
  }
  return JSON.stringify(value, null, 2) ?? "";
}

function parseJsonl(jsonl: string): ParsedLine[] {
  return jsonl
    .split(/\r?\n/)
    .map((line, index) => ({ text: line.trim(), index }))
    .filter(({ text }) => text.length > 0)
    .map(({ text, index }) => {
      const parsed: unknown = JSON.parse(text);
      if (!isRecord(parsed)) {
        throw new Error(`第 ${index + 1} 行不是 JSON object`);
      }
      return { line: index + 1, record: parsed };
    });
}

function roleLabel(role: string): string {
  if (role === "assistant") return "Assistant";
  if (role === "user") return "User";
  if (role === "tool") return "Tool";
  if (role === "system") return "System";
  return role || "Message";
}

function phaseLabel(record: JsonRecord): string {
  const metadata = isRecord(record.response_metadata)
    ? record.response_metadata
    : {};
  const phase = asString(metadata.phase);
  if (phase === "final_answer") return "最终回复";
  if (phase === "commentary") return "工具调用";
  if (phase === "text") return "文本";
  if (phase === "tool") return "工具";
  return phase;
}

function mergeReasoningBlocks(blocks: JsonRecord[]): JsonRecord[] {
  const merged: JsonRecord[] = [];

  for (const block of blocks) {
    if (asString(block.type) !== "reasoning") {
      merged.push(block);
      continue;
    }

    const previous = merged[merged.length - 1];
    if (previous && asString(previous.type) === "reasoning") {
      merged[merged.length - 1] = {
        ...previous,
        reasoning: `${asString(previous.reasoning)}${asString(block.reasoning)}`,
      };
      continue;
    }

    merged.push({ ...block });
  }

  return merged;
}

function renderContentBlock(block: JsonRecord, index: number) {
  const type = asString(block.type);
  const text =
    type === "reasoning"
      ? asString(block.reasoning)
      : type === "refusal"
        ? asString(block.refusal)
        : asString(block.text);
  const label =
    type === "reasoning"
      ? "推理"
      : type === "text"
        ? "正文"
        : type === "refusal"
          ? "拒绝"
          : type || "内容";

  return (
    <div key={`${type}-${index}`} className={`agent-state-block block-${type}`}>
      <div className="agent-state-block-label">{label}</div>
      <pre>{text || prettyJson(block)}</pre>
    </div>
  );
}

function renderContent(content: unknown) {
  if (Array.isArray(content)) {
    const blocks = mergeReasoningBlocks(content.filter(isRecord));
    if (blocks.length > 0) {
      return (
        <div className="agent-state-blocks">
          {blocks.map((block, index) => renderContentBlock(block, index))}
        </div>
      );
    }
  }

  const text = typeof content === "string" ? content : prettyJson(content);
  if (!text.trim()) {
    return null;
  }
  return (
    <div className="agent-state-blocks">
      <div className="agent-state-block block-text">
        <div className="agent-state-block-label">内容</div>
        <pre>{text}</pre>
      </div>
    </div>
  );
}

function renderToolCalls(toolCalls: unknown) {
  if (!Array.isArray(toolCalls) || toolCalls.length === 0) {
    return null;
  }

  return (
    <div className="agent-state-tools">
      {toolCalls.filter(isRecord).map((toolCall, index) => {
        const name = asString(toolCall.name) || "unknown_tool";
        const id = asString(toolCall.id);
        const args = toolCall.args ?? toolCall.arguments ?? {};
        return (
          <div key={`${id || name}-${index}`} className="agent-state-tool">
            <div className="agent-state-tool-head">
              <span className="agent-state-tool-name">{name}</span>
              {id ? <span className="agent-state-tool-id">{id}</span> : null}
            </div>
            <pre>{prettyJson(args)}</pre>
          </div>
        );
      })}
    </div>
  );
}

function AgentStateCard({ item }: { item: ParsedLine }) {
  const { record, line } = item;
  const role = asString(record.role);
  const type = asString(record.type);
  const phase = phaseLabel(record);
  const name = asString(record.name);
  const toolCallId = asString(record.tool_call_id);
  const content = renderContent(record.content);
  const toolCalls = renderToolCalls(record.tool_calls);

  return (
    <article className={`agent-state-card role-${role || "unknown"}`}>
      <div className="agent-state-card-head">
        <div className="agent-state-card-title">
          <span className="agent-state-role">{roleLabel(role)}</span>
          {type ? <span className="agent-state-type">{type}</span> : null}
        </div>
        <div className="agent-state-card-meta">
          {phase ? <span>{phase}</span> : null}
          {name ? <span>{name}</span> : null}
          <span>#{line}</span>
        </div>
      </div>

      {content}
      {toolCalls}

      {toolCallId ? (
        <div className="agent-state-tool-result-id">tool_call_id: {toolCallId}</div>
      ) : null}

      <details className="agent-state-raw">
        <summary>原始 JSON</summary>
        <pre>{prettyJson(record)}</pre>
      </details>
    </article>
  );
}

export default function AgentStatePanel({
  jsonl,
  messageCount,
  loadedAt,
  loading,
  error,
}: {
  jsonl: string;
  messageCount: number;
  loadedAt: string | null;
  loading: boolean;
  error: string | null;
}) {
  const loadedAtText = loadedAt ? formatTime(loadedAt) : "";
  let parsed: ParsedLine[] = [];
  let parseError = "";

  if (jsonl.trim()) {
    try {
      parsed = parseJsonl(jsonl);
    } catch (err) {
      parseError = err instanceof Error ? err.message : String(err);
    }
  }

  return (
    <section className="agent-state-panel">
      <div className="agent-state-header">
        <div className="agent-state-title">Agent State</div>
        <div className="agent-state-meta">
          <span>{messageCount} messages</span>
          {loadedAtText ? <span>{loadedAtText}</span> : null}
        </div>
      </div>
      {loading ? (
        <div className="empty-state">正在读取 Agent State...</div>
      ) : error ? (
        <div className="empty-state error-state">
          <div className="error-title">Agent State 加载失败</div>
          <div className="error-message">{error}</div>
        </div>
      ) : parseError ? (
        <div className="agent-state-fallback">
          <div className="empty-state error-state">
            <div className="error-title">Agent State 解析失败</div>
            <div className="error-message">{parseError}</div>
          </div>
          <pre className="agent-state-jsonl">{jsonl}</pre>
        </div>
      ) : parsed.length > 0 ? (
        <div className="agent-state-list">
          {parsed.map((item) => (
            <AgentStateCard key={item.line} item={item} />
          ))}
        </div>
      ) : (
        <div className="empty-state">暂无 Agent State messages</div>
      )}
    </section>
  );
}
