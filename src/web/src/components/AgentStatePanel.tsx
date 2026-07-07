import { formatDateTime } from "../utils/format";
import {
  buildAgentStateSummary,
  formatAgentStateJsonlForDisplay,
  parseAgentStateRecords,
  type AgentStateSummary,
} from "../state/agentStateDisplay";

function AgentStateKeyFlow({
  skills,
  customTools,
  customToolResults,
  finalText,
}: AgentStateSummary) {
  if (
    skills.length === 0 &&
    customTools.length === 0 &&
    customToolResults.length === 0 &&
    !finalText
  ) {
    return null;
  }

  return (
    <div className="agent-state-key-flow" aria-label="Agent State 关键链路">
      <div className="agent-state-key-flow-title">关键链路</div>
      <ol>
        {skills.length > 0 ? <li>读取 skill：{skills.join("、")}</li> : null}
        {customTools.length > 0 ? <li>目标扩展工具：{customTools.join("、")}</li> : null}
        {customToolResults.map((result) => (
          <li key={`${result.toolName}:result`}>
            扩展工具返回：{result.invocationToolName} -&gt; {result.toolName} -&gt; {result.resultText}
          </li>
        ))}
        {finalText ? <li>最终回复正文：{finalText}</li> : null}
      </ol>
    </div>
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
          <div>
            <span>最终文本</span>
            <strong
              aria-label={
                summary.finalText
                  ? `最终文本：${summary.finalText}`
                  : "最终文本：暂无最终回复"
              }
            >
              {summary.finalText || "暂无最终回复"}
            </strong>
          </div>
        </div>
      ) : null}
      {displayJsonl ? <AgentStateKeyFlow {...summary} /> : null}
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
