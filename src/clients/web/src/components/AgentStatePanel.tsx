import { formatDateTime } from "../utils/format";
import {
  buildAgentStateSummary,
  formatAgentStateJsonlForDisplay,
  parseAgentStateRecords,
} from "../state/agentStateDisplay";

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
  const records = trimmedJsonl ? parseAgentStateRecords(trimmedJsonl) : [];
  const summary = buildAgentStateSummary(records);

  return (
    <section className="agent-state-panel">
      <div className="agent-state-header">
        <div className="agent-state-title">Agent State 调试快照</div>
        <div className="agent-state-meta">
          <span>{messageCount} messages</span>
          {loadedAtText ? <span>{loadedAtText}</span> : null}
        </div>
      </div>
      <div className="agent-state-debug-note">
        这是用于排查 checkpoint 和消息格式的原始 JSONL 快照，不是普通对话视图。
      </div>
      {displayJsonl ? (
        <div className="agent-state-summary">
          <div>
            <span>Skill</span>
            <strong>{summary.skills.join("、") || "未检测到 skill 文件读取"}</strong>
          </div>
          <div>
            <span>扩展工具</span>
            <strong>{summary.customTools.join("、") || "未检测到扩展工具调用"}</strong>
          </div>
        </div>
      ) : null}
      {loading ? (
        <div className="empty-state">正在读取 Agent State...</div>
      ) : error ? (
        <div className="empty-state error-state">
          <div className="error-title">Agent State 加载失败</div>
          <div className="error-message">{error}</div>
        </div>
      ) : displayJsonl ? (
        <details className="agent-state-raw">
          <summary>原始 JSONL（调试）</summary>
          <pre
            className="agent-state-jsonl"
            aria-label="Agent State messages JSONL"
          >
            {displayJsonl}
          </pre>
        </details>
      ) : (
        <div className="empty-state">暂无 Agent State messages</div>
      )}
    </section>
  );
}
