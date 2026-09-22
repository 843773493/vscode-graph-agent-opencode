import { useState } from "react";
import { formatDateTime } from "../../../utils/format";
import AssemblyContextInspector from "../../contextInspection/AssemblyContextInspector";
import {
  agentStateInvalidLineNote,
  type AgentStateJsonlLine,
  agentStateRecordsFromLines,
  buildAgentStateSummary,
  formatAgentStateLinesForDisplay,
  parseAgentStateJsonlLines,
} from "../../../state/display/agentStateDisplay";

export default function AgentStatePanel({
  jsonl,
  messageCount,
  loadedAt,
  loading,
  error,
  port,
  workspaceId,
  sessionId,
  active,
}: {
  jsonl: string;
  messageCount: number;
  loadedAt: string | null;
  loading: boolean;
  error: string | null;
  port: number;
  workspaceId: string;
  sessionId: string;
  active: boolean;
}) {
  const [frozen, setFrozen] = useState(false);
  const loadedAtText = loadedAt ? formatDateTime(loadedAt) : "";
  const trimmedJsonl = jsonl.trim();
  // 解析一次，展示文本与摘要记录共用同一结果；非法行不会抛异常，而是标注在面板上。
  const parsedLines = trimmedJsonl ? parseAgentStateJsonlLines(trimmedJsonl) : [];
  const displayJsonl = formatAgentStateLinesForDisplay(parsedLines);
  const invalidLines = parsedLines.filter(
    (line): line is Extract<AgentStateJsonlLine, { ok: false }> => !line.ok,
  );
  const records = agentStateRecordsFromLines(parsedLines);
  const summary = buildAgentStateSummary(records);

  return (
    <section className="agent-state-panel">
      <div className="context-inspection-mode" aria-label="上下文检查模式">
        <button type="button" aria-pressed={!frozen} onClick={() => setFrozen(false)}>当前状态</button>
        <button type="button" aria-pressed={frozen} onClick={() => setFrozen(true)}>冻结请求</button>
      </div>
      {frozen ? (sessionId && workspaceId ? <AssemblyContextInspector
        key={`${workspaceId}:${sessionId}`} port={port} workspaceId={workspaceId}
        sessionId={sessionId} active={active} /> : <p>请先选择会话。</p>) : <>
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
      {invalidLines.length > 0 ? (
        <div className="agent-state-invalid-lines" role="alert">
          <strong>
            {invalidLines.length} 行 Agent State JSONL 无法解析，已按原文片段标注在下文快照中。
          </strong>
          <details>
            <summary>查看失败行与技术详情</summary>
            {invalidLines.map((line) => (
              <code key={line.lineNumber}>{agentStateInvalidLineNote(line)}</code>
            ))}
          </details>
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
      </>}
    </section>
  );
}
