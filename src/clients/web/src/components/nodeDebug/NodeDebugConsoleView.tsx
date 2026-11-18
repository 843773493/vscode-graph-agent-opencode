import { t, formatNumber } from "../../../../shared/core/i18n/index.js";
import { createContext, useContext, useEffect, useMemo, useRef } from "react";
import type { Dispatch, SetStateAction } from "react";

import type { NodeDebugState } from "../../types/backend";
import { formatLocalTime } from "../../utils/format";
import { nodeDebugActionActor } from "./nodeDebugPresentation";

/** 工作台注入原版调试渲染器；Web 产品不持有原生服务或第二套 ANSI 解析器。 */
export type DebugOutputRenderer = (container: HTMLElement, text: string) => () => void;
export const DebugOutputContext = createContext<DebugOutputRenderer | null>(null);

function ProgramOutput({ text }: { text: string }) {
  const render = useContext(DebugOutputContext);
  const host = useRef<HTMLSpanElement>(null);
  useEffect(() => {
    if (!render) throw new Error("工作台未提供调试输出渲染器");
    return render(host.current!, text);
  }, [render, text]);
  return <span ref={host}>{text}</span>;
}

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
  const actionLimit = extensionWindow ? 40 : 12;
  const recentActions = useMemo(
    () => (state?.actions ?? []).slice(-actionLimit).reverse(),
    [actionLimit, state?.actions],
  );
  const recentEvaluations = useMemo(
    () => (state?.evaluations ?? []).slice().reverse(),
    [state?.evaluations],
  );
  const outputText = useMemo(
    () => (state?.output ?? []).join("\n"),
    [state?.output],
  );

  return (
    <div className="node-debug-view node-debug-console-view" role="tabpanel">
      <section className="node-debug-section">
        <div className="debug-section-title-row"><h3>{t("debug.console.expression")}</h3><span>{status === "paused" ? t("debug.console.available") : t("debug.console.pauseRequired")}</span></div>
        <div className="node-debug-evaluate-row">
          <input
            value={expression}
            onChange={(event) => setExpression(event.target.value)}
            onKeyDown={(event) => { if (event.key === "Enter") onEvaluate(); }}
            placeholder={t("debug.console.example")}
            aria-label={t("debug.console.expressionAria")}
          />
          <button type="button" onClick={onEvaluate} disabled={actionBusy || status !== "paused"}>{t("debug.console.evaluate")}</button>
        </div>
        <div className="node-debug-evaluation-list">
          {(state?.evaluations ?? []).length === 0 ? <span className="debug-muted">{t("debug.console.noEvaluations")}</span> : null}
          {recentEvaluations.map((evaluation) => (
            <div key={`${evaluation.evaluated_at}-${evaluation.expression}`}>
              <code>{evaluation.expression}</code>
              <strong>{evaluation.error ?? evaluation.value ?? evaluation.description ?? "undefined"}</strong>
            </div>
          ))}
        </div>
      </section>
      <section className="node-debug-section">
        <div className="debug-section-title-row"><h3>{t("debug.console.title")}</h3><span>{t("debug.console.lines", formatNumber(state?.output?.length ?? 0))}</span></div>
        <pre className="node-debug-output">{outputText ? <ProgramOutput text={outputText} /> : t("debug.console.noOutput")}</pre>
      </section>
      <section className="node-debug-section">
        <div className="debug-section-title-row"><h3>{t("debug.console.actions")}</h3><span>{formatNumber(state?.actions?.length ?? 0)}</span></div>
        <div className="node-debug-action-list">
          {recentActions.length === 0 ? <span className="debug-muted">{t("debug.console.noActions")}</span> : null}
          {recentActions.map((action) => (
            <div key={action.action_id}>
              <span className={action.actor === "ai" ? "agent" : "human"}>{nodeDebugActionActor(action)}</span>
              <strong>{action.tool_name ?? action.action}</strong>
              <small title={action.message}>{action.message}</small>
              <time>{formatLocalTime(action.created_at)}</time>
            </div>
          ))}
        </div>
      </section>
    </div>
  );
}
