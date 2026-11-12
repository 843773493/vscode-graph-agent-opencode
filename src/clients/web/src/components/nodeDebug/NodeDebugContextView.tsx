import { t, formatNumber } from "../../../../shared/core/i18n/index.js";
import { memo, useMemo } from "react";
import type { Dispatch, SetStateAction } from "react";

import type { NodeDebugState, NodeDebugVariable } from "../../types/backend";

const GLOBAL_VARIABLE_PREVIEW_LIMIT = 80;
const EMPTY_NODE_DEBUG_VARIABLES: NodeDebugVariable[] = [];

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

const DebugVariableSection = memo(function DebugVariableSection({
  variables,
}: {
  variables: NodeDebugVariable[];
}) {
  const { localVariables, globalVariables } = useMemo(() => {
    const localVariables: NodeDebugVariable[] = [];
    const globalVariables: NodeDebugVariable[] = [];
    for (const variable of variables) {
      (variable.scope === "global" ? globalVariables : localVariables).push(variable);
    }
    return { localVariables, globalVariables };
  }, [variables]);
  const globalVariablePreview = useMemo(
    () => globalVariables.slice(0, GLOBAL_VARIABLE_PREVIEW_LIMIT),
    [globalVariables],
  );

  return (
    <section className="node-debug-section node-debug-variable-section">
      <div className="debug-section-title-row"><h3>{t("debug.context.locals")}</h3><span>{formatNumber(localVariables.length)}</span></div>
      {localVariables.length > 0 ? (
        <DebugVariableRows variables={localVariables} />
      ) : (
        <span className="debug-muted">{t("debug.context.noLocals")}</span>
      )}
      {globalVariables.length > 0 ? (
        <details className="node-debug-variable-group">
          <summary>{t("debug.context.globals")} <span>{formatNumber(globalVariables.length)}</span></summary>
          <DebugVariableRows variables={globalVariablePreview} />
          {globalVariables.length > GLOBAL_VARIABLE_PREVIEW_LIMIT ? (
            <small>{t("debug.context.globalLimit", formatNumber(GLOBAL_VARIABLE_PREVIEW_LIMIT))}</small>
          ) : null}
        </details>
      ) : null}
    </section>
  );
});

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
  const variables = activeFrame?.variables ?? EMPTY_NODE_DEBUG_VARIABLES;

  return (
    <div className="node-debug-view node-debug-context-view" role="tabpanel">
      <section className="node-debug-section">
        <div className="debug-section-title-row"><h3>{t("debug.context.stack")}</h3><span>{t("debug.context.frames", formatNumber(state?.call_stack?.length ?? 0))}</span></div>
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
        {!activeFrame ? <div className="debug-empty-state compact">{t("debug.context.noFrame")}</div> : null}
      </section>
      <section className="node-debug-section">
        <div className="debug-section-title-row"><h3>{t("debug.context.watch")}</h3><span>{status === "paused" ? t("debug.console.available") : t("debug.console.pauseRequired")}</span></div>
        <div className="node-debug-evaluate-row">
          <input value={expression} onChange={(event) => setExpression(event.target.value)} onKeyDown={(event) => { if (event.key === "Enter") onEvaluate(); }} placeholder={t("debug.context.expression")} />
          <button type="button" onClick={onEvaluate} disabled={actionBusy || status !== "paused"}>{t("debug.console.evaluate")}</button>
        </div>
        {state?.last_evaluation ? <pre>{state.last_evaluation.error ?? state.last_evaluation.value ?? state.last_evaluation.description ?? "undefined"}</pre> : null}
      </section>
      <DebugVariableSection variables={variables} />
    </div>
  );
}
