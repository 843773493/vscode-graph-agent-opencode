import { useCallback, useEffect, useRef, useState } from "react";
import { applyNodeDebugAction, getNodeDebugState, startNodeDebug } from "../../api";
import type { NodeDebugState } from "../../types/backend";

interface NodeDebugPanelProps {
  apiPort: number;
  workspaceId: string | null;
  sessionId: string | null;
  activeFilePath: string | null;
  onOpenWorkspacePath: (path: string) => Promise<void>;
  onStatusChange: (message: string) => void;
}

type NodeDebugAction =
  | "continue"
  | "pause"
  | "step_over"
  | "step_into"
  | "step_out"
  | "set_breakpoint"
  | "clear_breakpoint"
  | "evaluate"
  | "stop";

const FIXTURE_PATH = "debug/node-debug-fixture.mjs";

function statusLabel(status: NodeDebugState["status"]): string {
  return {
    idle: "未启动",
    starting: "启动中",
    running: "运行中",
    paused: "已暂停",
    exited: "已退出",
    failed: "失败",
  }[status];
}

export default function NodeDebugPanel({
  apiPort,
  workspaceId,
  sessionId,
  activeFilePath,
  onOpenWorkspacePath,
  onStatusChange,
}: NodeDebugPanelProps) {
  const [state, setState] = useState<NodeDebugState | null>(null);
  const [scriptPath, setScriptPath] = useState(FIXTURE_PATH);
  const [scriptArgs, setScriptArgs] = useState("7");
  const [breakpointLine, setBreakpointLine] = useState("5");
  const [expression, setExpression] = useState("doubled");
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [actionBusy, setActionBusy] = useState(false);
  const stateRequestVersionRef = useRef(0);
  const formSessionRef = useRef<string | null>(null);
  const actionInFlightRef = useRef(false);

  const refresh = useCallback(async () => {
    if (!sessionId) {
      setState(null);
      return;
    }
    const requestVersion = stateRequestVersionRef.current;
    const nextState = await getNodeDebugState(apiPort, sessionId, workspaceId);
    if (
      requestVersion === stateRequestVersionRef.current
      && !actionInFlightRef.current
    ) {
      setState(nextState);
    }
  }, [apiPort, sessionId, workspaceId]);

  useEffect(() => {
    const requestVersion = ++stateRequestVersionRef.current;
    setError(null);
    setState(null);
    formSessionRef.current = null;
    actionInFlightRef.current = false;
    if (!sessionId) {
      return;
    }
    let disposed = false;
    const poll = async () => {
      try {
        const nextState = await getNodeDebugState(apiPort, sessionId, workspaceId);
        if (
          !disposed
          && stateRequestVersionRef.current === requestVersion
          && !actionInFlightRef.current
        ) {
          setState(nextState);
          if (formSessionRef.current !== sessionId && nextState.status !== "idle") {
            setScriptPath(nextState.script_path ?? FIXTURE_PATH);
            setScriptArgs((nextState.args ?? []).join(" "));
            const firstBreakpoint = (nextState.breakpoints ?? [])[0];
            if (firstBreakpoint) setBreakpointLine(String(firstBreakpoint.line));
            formSessionRef.current = sessionId;
          }
        }
      } catch (cause: unknown) {
        if (!disposed) setError(cause instanceof Error ? cause.message : String(cause));
      }
    };
    void poll();
    const intervalId = window.setInterval(() => void poll(), 800);
    return () => {
      disposed = true;
      window.clearInterval(intervalId);
    };
  }, [apiPort, sessionId, workspaceId]);

  const runAction = useCallback(async (
    action: NodeDebugAction,
    params: Record<string, unknown> = {},
  ): Promise<boolean> => {
    if (!sessionId) return false;
    const actionVersion = ++stateRequestVersionRef.current;
    actionInFlightRef.current = true;
    setActionBusy(true);
    setError(null);
    try {
      const nextState = await applyNodeDebugAction(
        apiPort,
        { session_id: sessionId, action, params },
        workspaceId,
      );
      if (actionVersion === stateRequestVersionRef.current) setState(nextState);
      onStatusChange(`Node 调试：${action}`);
      return true;
    } catch (cause: unknown) {
      const message = cause instanceof Error ? cause.message : String(cause);
      setError(message);
      onStatusChange(`Node 调试动作失败: ${message}`);
      try {
        const authoritativeState = await getNodeDebugState(apiPort, sessionId, workspaceId);
        if (actionVersion === stateRequestVersionRef.current) setState(authoritativeState);
      } catch (refreshCause: unknown) {
        const refreshMessage = refreshCause instanceof Error
          ? refreshCause.message
          : String(refreshCause);
        setError(`${message}；重新获取调试状态失败: ${refreshMessage}`);
      }
      return false;
    } finally {
      actionInFlightRef.current = false;
      setActionBusy(false);
      if (stateRequestVersionRef.current === actionVersion) {
        stateRequestVersionRef.current += 1;
      }
    }
  }, [apiPort, onStatusChange, sessionId, workspaceId]);

  const start = async () => {
    if (!sessionId) return;
    const line = Number(breakpointLine);
    if (!Number.isSafeInteger(line) || line < 1) {
      setError("启动调试前，断点行号必须是正整数");
      return;
    }
    const actionVersion = ++stateRequestVersionRef.current;
    actionInFlightRef.current = true;
    setLoading(true);
    setError(null);
    try {
      const args = scriptArgs.trim() ? scriptArgs.trim().split(/\s+/u) : [];
      const nextState = await startNodeDebug(
        apiPort,
        {
          session_id: sessionId,
          path: scriptPath.trim(),
          args,
          breakpoints: [{ path: scriptPath.trim(), line }],
        },
        workspaceId,
      );
      if (actionVersion === stateRequestVersionRef.current) setState(nextState);
      onStatusChange(`已启动 Node 调试: ${scriptPath.trim()}`);
    } catch (cause: unknown) {
      const message = cause instanceof Error ? cause.message : String(cause);
      setError(message);
      onStatusChange(`启动 Node 调试失败: ${message}`);
    } finally {
      actionInFlightRef.current = false;
      setActionBusy(false);
      if (stateRequestVersionRef.current === actionVersion) {
        stateRequestVersionRef.current += 1;
      }
      setLoading(false);
    }
  };

  const setBreakpoint = () => {
    const line = Number(breakpointLine);
    if (!Number.isSafeInteger(line) || line < 1) {
      setError("断点行号必须是正整数");
      return;
    }
    void runAction("set_breakpoint", { path: scriptPath.trim(), line });
  };

  const evaluate = () => {
    if (!expression.trim()) return;
    void runAction("evaluate", { expression: expression.trim() });
  };

  const status = state?.status ?? "idle";
  const activeFrame = state?.call_stack?.[0] ?? null;
  const activeScriptPath = state?.script_path ?? scriptPath;
  const activeFrameScopeNames = activeFrame?.scope_names ?? [];
  const activeFrameVariables = activeFrame?.variables ?? [];
  const processCanStop = status === "starting" || status === "running" || status === "paused";

  return (
    <section className="node-debug-panel" aria-label="Node 源码调试">
      <header className="node-debug-header">
        <div>
          <strong>Node 源码调试</strong>
          <span>{sessionId ? `${statusLabel(status)} · 独立于 Agent 执行状态` : "选择会话后可启动"}</span>
        </div>
        <button type="button" onClick={() => void refresh()} disabled={!sessionId}>刷新</button>
      </header>

      <div className="node-debug-start-form">
        <label>
          脚本路径
          <input value={scriptPath} onChange={(event) => setScriptPath(event.target.value)} placeholder={FIXTURE_PATH} />
        </label>
        <label>
          参数
          <input value={scriptArgs} onChange={(event) => setScriptArgs(event.target.value)} placeholder="7" />
        </label>
        <div className="node-debug-actions-row">
          <button type="button" onClick={() => void start()} disabled={!sessionId || loading}>{loading ? "启动中" : "启动 / 重启"}</button>
          {activeScriptPath ? <button type="button" onClick={() => void onOpenWorkspacePath(activeScriptPath)}>打开源码</button> : null}
        </div>
      </div>

      <div className="node-debug-controls">
        <button type="button" onClick={() => void runAction("continue")} disabled={actionBusy || status !== "paused"}>继续</button>
        <button type="button" onClick={() => void runAction("pause")} disabled={actionBusy || status !== "running"}>暂停</button>
        <button type="button" onClick={() => void runAction("step_over")} disabled={actionBusy || status !== "paused"}>单步跳过</button>
        <button type="button" onClick={() => void runAction("step_into")} disabled={actionBusy || status !== "paused"}>单步进入</button>
        <button type="button" onClick={() => void runAction("step_out")} disabled={actionBusy || status !== "paused"}>单步跳出</button>
        <button type="button" onClick={() => void runAction("stop")} disabled={actionBusy || !processCanStop}>停止</button>
      </div>

      <section className="node-debug-section" aria-label="Node 源码断点">
        <div className="debug-section-title-row"><h3>源码断点</h3><span>{state?.breakpoints?.length ?? 0} 个</span></div>
        <div className="node-debug-breakpoint-form">
          <input value={breakpointLine} onChange={(event) => setBreakpointLine(event.target.value)} inputMode="numeric" aria-label="源码断点行号" />
          <button
            type="button"
            onClick={setBreakpoint}
            disabled={!sessionId || !["starting", "running", "paused"].includes(status)}
          >
            设置行断点
          </button>
        </div>
        {(state?.breakpoints ?? []).map((breakpoint) => (
          <div className="node-debug-breakpoint-row" key={breakpoint.breakpoint_id}>
            <span className={breakpoint.verified ? "verified" : "unverified"}>●</span>
            <button type="button" onClick={() => void onOpenWorkspacePath(breakpoint.path)}>{breakpoint.path}:{breakpoint.actual_line ?? breakpoint.line}</button>
            <button type="button" onClick={() => void runAction("clear_breakpoint", { breakpoint_id: breakpoint.breakpoint_id })}>×</button>
          </div>
        ))}
      </section>

      <section className="node-debug-section" aria-label="调用栈和变量">
        <div className="debug-section-title-row"><h3>调用栈</h3><span>{state?.call_stack?.length ?? 0} 帧</span></div>
        {activeFrame ? (
          <>
            <div className="node-debug-frame-card">
              <strong>{activeFrame.function_name}</strong>
              <button type="button" onClick={() => activeFrame.path && void onOpenWorkspacePath(activeFrame.path)}>{activeFrame.path ?? activeFrame.url}:{activeFrame.line}</button>
              <small>{activeFrameScopeNames.join(" · ")}</small>
            </div>
            <details open className="node-debug-variables">
              <summary>局部变量（{activeFrameVariables.length}）</summary>
              {activeFrameVariables.map((variable) => <div key={`${variable.name}-${variable.object_id ?? variable.value}`}><span>{variable.name}</span><code>{variable.value}</code></div>)}
            </details>
          </>
        ) : <div className="debug-empty-state compact">命中源码断点后显示调用栈和局部变量。</div>}
      </section>

      <section className="node-debug-section" aria-label="表达式求值">
        <div className="debug-section-title-row"><h3>Watch / 求值</h3><span>{status === "paused" ? "可用" : "需暂停"}</span></div>
        <div className="node-debug-evaluate-row">
          <input value={expression} onChange={(event) => setExpression(event.target.value)} placeholder="输入表达式" />
          <button type="button" onClick={evaluate} disabled={actionBusy || status !== "paused"}>求值</button>
        </div>
        {state?.last_evaluation ? <pre>{state.last_evaluation.error ?? state.last_evaluation.value ?? state.last_evaluation.description ?? "undefined"}</pre> : null}
      </section>

      <section className="node-debug-section" aria-label="Node 输出">
        <div className="debug-section-title-row"><h3>程序输出</h3><span>{state?.output?.length ?? 0} 行</span></div>
        <pre className="node-debug-output">{(state?.output ?? []).join("\n") || "暂无输出"}</pre>
      </section>

      {state?.error_message ? <div className="debug-error" role="alert">{state.error_message}</div> : null}
      {error ? <div className="debug-error" role="alert">{error}</div> : null}
      {activeFilePath && activeFilePath !== activeScriptPath ? <small className="node-debug-active-file">当前编辑器文件：{activeFilePath}</small> : null}
    </section>
  );
}
