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

  return (
    <div className="node-debug-view node-debug-source-view" role="tabpanel">
      {!sourcePath ? (
        <div className="debug-empty-state compact" role="status">
          <span>尚未选择 JavaScript 入口。</span>
          <button type="button" onClick={onShowConfiguration}>配置调试入口</button>
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
      {status === "paused" && activeFrame ? (
        <div className="debug-empty-state compact" role="status">
          <span>已暂停在 {activeFrame.path ?? activeFrame.url}:{activeFrame.line}，{localVariableCount} 个局部变量。</span>
          <button type="button" onClick={onShowContext}>查看调用栈与变量</button>
        </div>
      ) : null}
      {status === "exited" && (state?.output?.length ?? 0) > 0 ? (
        <div className="debug-empty-state compact" role="status">
          <span>调试已结束，保留 {state?.output?.length ?? 0} 行程序输出。</span>
          <button type="button" onClick={onShowConsole}>查看控制台</button>
        </div>
      ) : null}
      <details className="node-debug-secondary" open={extensionWindow}>
        <summary>断点列表与高级设置 <span>{breakpoints.length}</span></summary>
        <div className="node-debug-breakpoint-form">
          <input value={breakpointLine} onChange={(event) => setBreakpointLine(event.target.value)} inputMode="numeric" placeholder="行" aria-label="源码断点行号" />
          <input value={breakpointCondition} onChange={(event) => setBreakpointCondition(event.target.value)} placeholder="条件（可选）" aria-label="源码断点条件" />
          <button type="button" onClick={onAddBreakpoint} disabled={!sessionId || actionBusy}>添加</button>
        </div>
        <div className="node-debug-breakpoint-list">
          {breakpoints.length === 0 ? <span className="debug-muted">尚未设置源码断点</span> : null}
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
                {breakpoint.relocation_status === "relocated" ? " · 已重定位" : ""}
                {breakpoint.relocation_status === "pending_update" ? " · 待更新" : ""}
                {breakpoint.relocation_status === "source_deleted" ? " · 文件已删除" : ""}
              </button>
              <button type="button" onClick={() => onClearBreakpoint(breakpoint.breakpoint_id)} aria-label="清除断点">
                <span className="codicon codicon-close" aria-hidden="true" />
              </button>
            </div>
          ))}
        </div>
      </details>
    </div>
  );
}
