import type { Dispatch, SetStateAction } from "react";

import type { NodeDebugState, NodeDebugVariable } from "../../types/backend";

const GLOBAL_VARIABLE_PREVIEW_LIMIT = 80;

function DebugVariableRows({ variables }: { variables: NodeDebugVariable[] }) {
  return (
    <div className="node-debug-variable-list">
      {variables.map((variable) => (
        <div key={`${variable.scope}-${variable.name}-${variable.object_id ?? variable.value}`}>
          <span>{variable.name}</span>
          <code title={variable.value}>{variable.value}</code>
          <small>{variable.type ?? variable.scope}</small>
        </div>
      ))}
    </div>
  );
}

export default function NodeDebugContextView({
  state,
  status,
  actionBusy,
  expression,
  setExpression,
  onEvaluate,
  onOpenWorkspacePath,
}: {
  state: NodeDebugState | null;
  status: NodeDebugState["status"];
  actionBusy: boolean;
  expression: string;
  setExpression: Dispatch<SetStateAction<string>>;
  onEvaluate: () => void;
  onOpenWorkspacePath: (path: string) => Promise<void>;
}) {
  const activeFrame = state?.call_stack?.[0] ?? null;
  const localVariables = (activeFrame?.variables ?? []).filter(
    (variable) => variable.scope !== "global",
  );
  const globalVariables = (activeFrame?.variables ?? []).filter(
    (variable) => variable.scope === "global",
  );

  return (
    <div className="node-debug-view node-debug-context-view" role="tabpanel">
      <section className="node-debug-section">
        <div className="debug-section-title-row"><h3>调用栈</h3><span>{state?.call_stack?.length ?? 0} 帧</span></div>
        {(state?.call_stack ?? []).map((frame, index) => (
          <button
            type="button"
            className={`node-debug-frame-card${index === 0 ? " active" : ""}`}
            onClick={() => frame.path && void onOpenWorkspacePath(frame.path)}
            key={frame.call_frame_id}
          >
            <strong>{frame.function_name || "(anonymous)"}</strong>
            <span>{frame.path ?? frame.url}:{frame.line}</span>
          </button>
        ))}
        {!activeFrame ? <div className="debug-empty-state compact">命中源码断点后显示调用栈和变量。</div> : null}
      </section>
      <section className="node-debug-section">
        <div className="debug-section-title-row"><h3>Watch / 求值</h3><span>{status === "paused" ? "可用" : "需暂停"}</span></div>
        <div className="node-debug-evaluate-row">
          <input value={expression} onChange={(event) => setExpression(event.target.value)} onKeyDown={(event) => { if (event.key === "Enter") onEvaluate(); }} placeholder="输入表达式" />
          <button type="button" onClick={onEvaluate} disabled={actionBusy || status !== "paused"}>求值</button>
        </div>
        {state?.last_evaluation ? <pre>{state.last_evaluation.error ?? state.last_evaluation.value ?? state.last_evaluation.description ?? "undefined"}</pre> : null}
      </section>
      <section className="node-debug-section node-debug-variable-section">
        <div className="debug-section-title-row"><h3>局部变量</h3><span>{localVariables.length}</span></div>
        {localVariables.length > 0 ? (
          <DebugVariableRows variables={localVariables} />
        ) : (
          <span className="debug-muted">当前栈帧没有可展示的局部变量</span>
        )}
        {globalVariables.length > 0 ? (
          <details className="node-debug-variable-group">
            <summary>全局变量 <span>{globalVariables.length}</span></summary>
            <DebugVariableRows variables={globalVariables.slice(0, GLOBAL_VARIABLE_PREVIEW_LIMIT)} />
            {globalVariables.length > GLOBAL_VARIABLE_PREVIEW_LIMIT ? (
              <small>仅显示前 {GLOBAL_VARIABLE_PREVIEW_LIMIT} 项；可在上方 Watch 中按名称求值。</small>
            ) : null}
          </details>
        ) : null}
      </section>
    </div>
  );
}
