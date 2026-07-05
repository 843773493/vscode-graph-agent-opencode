import { formatDateTime } from "../utils/format";

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function summarizeDataUrl(value: string): string {
  if (!value.startsWith("data:")) {
    return value;
  }

  const commaIndex = value.indexOf(",");
  if (commaIndex === -1) {
    return value;
  }

  const header = value.slice(0, commaIndex);
  if (
    !header.startsWith("data:image/") &&
    !header.startsWith("data:video/") &&
    !header.startsWith("data:audio/")
  ) {
    return value;
  }

  return `${header},<redacted ${value.length - commaIndex - 1} chars>`;
}

function sanitizeAgentStateValue(value: unknown): unknown {
  if (typeof value === "string") {
    return summarizeDataUrl(value);
  }
  if (Array.isArray(value)) {
    return value.map(sanitizeAgentStateValue);
  }
  if (isRecord(value)) {
    return Object.fromEntries(
      Object.entries(value).map(([key, item]) => [
        key,
        sanitizeAgentStateValue(item),
      ]),
    );
  }
  return value;
}

function formatAgentStateJsonlForDisplay(jsonl: string): string {
  return jsonl
    .trim()
    .split(/\r?\n/)
    .filter((line) => line.trim().length > 0)
    .map((line) => {
      const parsed: unknown = JSON.parse(line);
      return JSON.stringify(sanitizeAgentStateValue(parsed));
    })
    .join("\n");
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
  const loadedAtText = loadedAt ? formatDateTime(loadedAt) : "";
  const trimmedJsonl = jsonl.trim();
  const displayJsonl = trimmedJsonl
    ? formatAgentStateJsonlForDisplay(trimmedJsonl)
    : "";

  return (
    <section className="agent-state-panel">
      <div className="agent-state-header">
        <div className="agent-state-title">Agent State messages</div>
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
      ) : displayJsonl ? (
        <pre
          className="agent-state-jsonl"
          aria-label="Agent State messages JSONL"
        >
          {displayJsonl}
        </pre>
      ) : (
        <div className="empty-state">暂无 Agent State messages</div>
      )}
    </section>
  );
}
