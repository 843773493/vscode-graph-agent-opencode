import type { Dispatch, SetStateAction } from "react";

import type { NodeDebugState } from "../../types/backend";
import { nodeDebugActionActor } from "./nodeDebugPresentation";

export default function NodeDebugConsoleView({
  state,
  status,
  actionBusy,
  extensionWindow,
  expression,
  setExpression,
  onEvaluate,
}: {
  state: NodeDebugState | null;
  status: NodeDebugState["status"];
  actionBusy: boolean;
  extensionWindow: boolean;
  expression: string;
  setExpression: Dispatch<SetStateAction<string>>;
  onEvaluate: () => void;
}) {
  const recentActions = [...(state?.actions ?? [])]
    .reverse()
    .slice(0, extensionWindow ? 40 : 12);

  return (
    <div className="node-debug-view node-debug-console-view" role="tabpanel">
      <section className="node-debug-section">
        <div className="debug-section-title-row"><h3>表达式控制台</h3><span>{status === "paused" ? "可用" : "需暂停"}</span></div>
        <div className="node-debug-evaluate-row">
          <input
            value={expression}
            onChange={(event) => setExpression(event.target.value)}
            onKeyDown={(event) => { if (event.key === "Enter") onEvaluate(); }}
            placeholder="例如 counter += 1"
            aria-label="调试控制台表达式"
          />
          <button type="button" onClick={onEvaluate} disabled={actionBusy || status !== "paused"}>求值</button>
        </div>
        <div className="node-debug-evaluation-list">
          {(state?.evaluations ?? []).length === 0 ? <span className="debug-muted">暂无表达式求值</span> : null}
          {[...(state?.evaluations ?? [])].reverse().map((evaluation) => (
            <div key={`${evaluation.evaluated_at}-${evaluation.expression}`}>
              <code>{evaluation.expression}</code>
              <strong>{evaluation.error ?? evaluation.value ?? evaluation.description ?? "undefined"}</strong>
            </div>
          ))}
        </div>
      </section>
      <section className="node-debug-section">
        <div className="debug-section-title-row"><h3>调试控制台</h3><span>{state?.output?.length ?? 0} 行</span></div>
        <pre className="node-debug-output">{(state?.output ?? []).join("\n") || "暂无程序输出"}</pre>
      </section>
      <section className="node-debug-section">
        <div className="debug-section-title-row"><h3>模型 / 用户动作</h3><span>{state?.actions?.length ?? 0}</span></div>
        <div className="node-debug-action-list">
          {recentActions.length === 0 ? <span className="debug-muted">暂无调试动作</span> : null}
          {recentActions.map((action) => (
            <div key={action.action_id}>
              <span className={action.actor === "ai" ? "agent" : "human"}>{nodeDebugActionActor(action)}</span>
              <strong>{action.tool_name ?? action.action}</strong>
              <small title={action.message}>{action.message}</small>
              <time>{new Date(action.created_at).toLocaleTimeString()}</time>
            </div>
          ))}
        </div>
      </section>
    </div>
  );
}
