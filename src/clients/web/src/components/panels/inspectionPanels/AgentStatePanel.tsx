import { t, formatNumber } from "../../../../../shared/core/i18n/index.js";
import { useMemo, useState } from "react";
import { formatDateTime } from "../../../utils/format";
import type { SessionContextSession } from "../../../api/session/sessionContext";
import AssemblyContextInspector from "../../contextInspection/AssemblyContextInspector";
import {
  agentStateInvalidLineNote,
  buildAgentStatePanelProjection,
} from "../../../state/display/agentStateDisplay";

export default function AgentStatePanel({
  jsonl,
  messageCount,
  loadedAt,
  loading,
  error,
  port,
  workspaceId,
  session,
  active,
}: {
  jsonl: string;
  messageCount: number;
  loadedAt: string | null;
  loading: boolean;
  error: string | null;
  port: number;
  workspaceId: string;
  session: SessionContextSession | null;
  active: boolean;
}) {
  const [frozen, setFrozen] = useState(false);
  const loadedAtText = loadedAt ? formatDateTime(loadedAt) : "";
  // 合并快照解析、错误行筛选与展示文本生成，切换冻结模式不重复遍历 JSONL。
  const { displayJsonl, invalidLines, summary } = useMemo(() => {
    return buildAgentStatePanelProjection(jsonl);
  }, [jsonl]);

  return (
    <section className="agent-state-panel">
      <div className="context-inspection-mode" aria-label={t("inspection.state.mode")}>
        <button type="button" aria-pressed={!frozen} onClick={() => setFrozen(false)}>{t("inspection.state.current")}</button>
        <button type="button" aria-pressed={frozen} onClick={() => setFrozen(true)}>{t("inspection.context.request")}</button>
      </div>
      {frozen ? (session && workspaceId ? <AssemblyContextInspector
        key={`${workspaceId}:${session.session_id}`} port={port} gatewayWorkspaceId={workspaceId}
        session={session} active={active} /> : <p>{t("inspection.state.selectSession")}</p>) : <>
      <div className="agent-state-header">
        <div className="agent-state-title">{t("inspection.state.title")}</div>
        <div className="agent-state-meta">
          <span>{t("inspection.state.messages", formatNumber(messageCount))}</span>
          {loadedAtText ? <span>{loadedAtText}</span> : null}
        </div>
      </div>
      <div className="agent-state-debug-note">

        {t("inspection.state.hint")}
      </div>
      {displayJsonl ? (
        <div className="agent-state-summary">
          <div>
            <span>{t("inspection.request.skillLoads")}</span>
            <strong>{summary.acceptedSkillLoads.join("、") || t("inspection.state.noSkill")}</strong>
          </div>
          <div>
            <span>{t("inspection.state.extensionTools")}</span>
            <strong>{summary.customTools.join("、") || t("inspection.state.noTools")}</strong>
          </div>
        </div>
      ) : null}
      {invalidLines.length > 0 ? (
        <div className="agent-state-invalid-lines" role="alert">
          <strong>
            {t("inspection.state.invalidLines", formatNumber(invalidLines.length))}
          </strong>
          <details>
            <summary>{t("inspection.state.invalidDetails")}</summary>
            {invalidLines.map((line) => (
              <code key={line.lineNumber}>{agentStateInvalidLineNote(line)}</code>
            ))}
          </details>
        </div>
      ) : null}
      {loading ? (
        <div className="empty-state">{t("inspection.state.loading")}</div>
      ) : error ? (
        <div className="empty-state error-state">
          <div className="error-title">{t("inspection.state.failed")}</div>
          <div className="error-message">{error}</div>
        </div>
      ) : displayJsonl ? (
        <details className="agent-state-raw">
          <summary>{t("inspection.state.raw")}</summary>
          <pre
            className="agent-state-jsonl"
            aria-label={t("inspection.state.rawAria")}
          >
            {displayJsonl}
          </pre>
        </details>
      ) : (
        <div className="empty-state">{t("inspection.state.empty")}</div>
      )}
      </>}
    </section>
  );
}
