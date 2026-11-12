import { t, formatNumber } from "../../../../shared/core/i18n/index.js";
import type { Dispatch, SetStateAction } from "react";

import type { NodeDebugBreakpoint, NodeDebugState } from "../../types/backend";
import {
  nodeDebugBreakpointLabel,
  type NodeDebugBreakpointDefinition,
} from "./NodeDebugBreakpointGutter";
import NodeDebugSourcePreview from "./NodeDebugSourcePreview";

export default function NodeDebugSourceView({
  apiPort,
  workspaceId,
  sessionId,
  state,
  status,
  sourcePath,
  sourceFocusLine,
  breakpoints,
  actionBusy,
  extensionWindow,
  breakpointLine,
  setBreakpointLine,
  breakpointCondition,
  setBreakpointCondition,
  onChangeBreakpoint,
  onAddBreakpoint,
  onClearBreakpoint,
  onSelectSource,
  onShowConfiguration,
  onShowContext,
  onShowConsole,
  onOpenWorkspacePath,
}: {
  apiPort: number;
  workspaceId: string | null;
  sessionId: string | null;
  state: NodeDebugState | null;
  status: NodeDebugState["status"];
  sourcePath: string | null;
  sourceFocusLine: number | null;
  breakpoints: NodeDebugBreakpoint[];
  actionBusy: boolean;
  extensionWindow: boolean;
  breakpointLine: string;
  setBreakpointLine: Dispatch<SetStateAction<string>>;
  breakpointCondition: string;
  setBreakpointCondition: Dispatch<SetStateAction<string>>;
  onChangeBreakpoint: (
    path: string,
    line: number,
    breakpointId: string | null,
    definition: NodeDebugBreakpointDefinition | null,
  ) => void;
  onAddBreakpoint: () => void;
  onClearBreakpoint: (breakpointId: string) => void;
  onSelectSource: (path: string, line: number) => void;
  onShowConfiguration: () => void;
  onShowContext: () => void;
  onShowConsole: () => void;
  onOpenWorkspacePath: (path: string) => Promise<void>;
}) {
  const activeFrame = state?.call_stack?.[0] ?? null;
  const localVariableCount = (activeFrame?.variables ?? []).filter(
    (variable) => variable.scope !== "global",
  ).length;
  const outputLineCount = state?.output?.length ?? 0;
  const sourceNotice = status === "paused" && activeFrame
    ? {
      message: t("debug.source.paused", activeFrame.path ?? activeFrame.url, activeFrame.line, formatNumber(localVariableCount)),
      actionLabel: t("debug.source.context"),
      onAction: onShowContext,
    }
    : status === "exited" && outputLineCount > 0
      ? {
        message: t("debug.source.exited", formatNumber(outputLineCount)),
        actionLabel: t("debug.source.console"),
        onAction: onShowConsole,
      }
      : null;

  return (
    <div className="node-debug-view node-debug-source-view" role="tabpanel">
      {!sourcePath ? (
        <div className="debug-empty-state compact" role="status">
          <span>{t("debug.source.noEntry")}</span>
          <button type="button" onClick={onShowConfiguration}>{t("debug.source.configure")}</button>
        </div>
      ) : null}
      <NodeDebugSourcePreview
        apiPort={apiPort}
        workspaceId={workspaceId}
        path={sourcePath}
        focusLine={sourceFocusLine}
        sourceRevision={state?.configuration_revision ?? 0}
        breakpoints={breakpoints}
        disabled={!sessionId || actionBusy}
        onChangeBreakpoint={onChangeBreakpoint}
        onOpenWorkspacePath={onOpenWorkspacePath}
      />
      <div className="node-debug-source-status" aria-live="polite">
        {sourceNotice ? (
          <div role="status">
            <span className="node-debug-source-status-message" title={sourceNotice.message}>
              {sourceNotice.message}
            </span>
            <button type="button" onClick={sourceNotice.onAction}>{sourceNotice.actionLabel}</button>
          </div>
        ) : null}
      </div>
      <details className="node-debug-secondary" open={extensionWindow}>
        <summary>{t("debug.source.breakpoints")} <span>{formatNumber(breakpoints.length)}</span></summary>
        <div className="node-debug-breakpoint-form">
          <input value={breakpointLine} onChange={(event) => setBreakpointLine(event.target.value)} inputMode="numeric" placeholder={t("debug.source.line")} aria-label={t("debug.source.lineAria")} />
          <input value={breakpointCondition} onChange={(event) => setBreakpointCondition(event.target.value)} placeholder={t("debug.source.conditionOptional")} aria-label={t("debug.source.conditionAria")} />
          <button type="button" onClick={onAddBreakpoint} disabled={!sessionId || actionBusy}>{t("debug.source.add")}</button>
        </div>
        <div className="node-debug-breakpoint-list">
          {breakpoints.length === 0 ? <span className="debug-muted">{t("debug.source.noBreakpoints")}</span> : null}
          {breakpoints.map((breakpoint) => (
            <div className="node-debug-breakpoint-row" key={breakpoint.breakpoint_id}>
              <span className={breakpoint.relocation_status === "pending_update" || breakpoint.relocation_status === "source_deleted" ? "stale" : breakpoint.verified ? "verified" : "unverified"} aria-hidden="true" />
              <button
                type="button"
                onClick={() => onSelectSource(breakpoint.path, breakpoint.line)}
                title={breakpoint.relocation_message ?? breakpoint.path}
              >
                {breakpoint.path}:{breakpoint.line}
                {` · ${nodeDebugBreakpointLabel(breakpoint)}`}
                {breakpoint.relocation_status === "relocated" ? t("debug.source.relocated") : ""}
                {breakpoint.relocation_status === "pending_update" ? t("debug.source.pendingUpdate") : ""}
                {breakpoint.relocation_status === "source_deleted" ? t("debug.source.deleted") : ""}
              </button>
              <button type="button" onClick={() => onClearBreakpoint(breakpoint.breakpoint_id)} aria-label={t("debug.source.clear")}>
                <span className="codicon codicon-close" aria-hidden="true" />
              </button>
            </div>
          ))}
        </div>
      </details>
    </div>
  );
}
