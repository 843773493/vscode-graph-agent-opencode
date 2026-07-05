import { formatDateTime } from "../utils/format";

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
      ) : trimmedJsonl ? (
        <pre
          className="agent-state-jsonl"
          aria-label="Agent State messages JSONL"
        >
          {trimmedJsonl}
        </pre>
      ) : (
        <div className="empty-state">暂无 Agent State messages</div>
      )}
    </section>
  );
}
