import { useCallback, useEffect, useMemo, useState } from "react";
import { controlJob, getJobDebugState } from "../../api";
import NodeDebugPanel from "./NodeDebugPanel";
import type {
  AgentDebugState,
  ControlAction,
  DebugBreakpoint,
  DebugStopSnapshot,
  JobControlRequest,
  TraceEvent,
} from "../../types/backend";

interface DebugPanelProps {
  apiPort: number;
  workspaceId: string | null;
  sessionId: string | null;
  activeFilePath: string | null;
  jobId: string | null;
  traceEvents: TraceEvent[];
  onOpenWorkspacePath: (path: string) => Promise<void>;
  onStatusChange: (message: string) => void;
}

type BreakpointKind = NonNullable<DebugBreakpoint["kind"]>;
type DebugMode = AgentDebugState["mode"];

const BREAKPOINT_OPTIONS: ReadonlyArray<{
  value: BreakpointKind;
  label: string;
  description: string;
}> = [
  { value: "tool_before", label: "工具前", description: "工具真正执行前暂停" },
  { value: "tool_after", label: "工具后", description: "工具返回结果后暂停" },
  { value: "llm_before", label: "模型前", description: "下一次模型请求边界暂停" },
];

const MODE_OPTIONS: ReadonlyArray<{ value: DebugMode; label: string }> = [
  { value: "ai", label: "AI 驱动" },
  { value: "collaborative", label: "协同模式" },
  { value: "human", label: "人类驱动" },
];

function breakpointLabel(kind: BreakpointKind): string {
  return BREAKPOINT_OPTIONS.find((item) => item.value === kind)?.label ?? kind;
}

function stopLabel(stop: DebugStopSnapshot): string {
  if (stop.point === "tool_before") return `工具前 · ${stop.tool_name ?? "未知工具"}`;
  if (stop.point === "tool_after") return `工具后 · ${stop.tool_name ?? "未知工具"}`;
  return "下一次模型请求前";
}

function displayJson(value: unknown): string {
  return JSON.stringify(value, null, 2) ?? "{}";
}

export default function DebugPanel({
  apiPort,
  workspaceId,
  sessionId,
  activeFilePath,
  jobId,
  traceEvents,
  onOpenWorkspacePath,
  onStatusChange,
}: DebugPanelProps) {
  const [debugState, setDebugState] = useState<AgentDebugState | null>(null);
  const [breakpointKind, setBreakpointKind] = useState<BreakpointKind>("tool_before");
  const [toolName, setToolName] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    if (!jobId) {
      setDebugState(null);
      return;
    }
    const nextState = await getJobDebugState(apiPort, jobId, workspaceId);
    setDebugState(nextState);
  }, [apiPort, jobId, workspaceId]);

  useEffect(() => {
    setError(null);
    if (!jobId) {
      setDebugState(null);
      return;
    }
    let disposed = false;
    const load = async () => {
      try {
        if (!disposed) setLoading(true);
        await refresh();
      } catch (cause: unknown) {
        if (!disposed) {
          const message = cause instanceof Error ? cause.message : String(cause);
          setError(message);
        }
      } finally {
        if (!disposed) setLoading(false);
      }
    };
    void load();
    const intervalId = window.setInterval(() => {
      void refresh().catch((cause: unknown) => {
        if (!disposed) {
          setError(cause instanceof Error ? cause.message : String(cause));
        }
      });
    }, 1200);
    return () => {
      disposed = true;
      window.clearInterval(intervalId);
    };
  }, [jobId, refresh]);

  const sendAction = useCallback(async (
    action: ControlAction,
    params: Record<string, unknown> = {},
  ): Promise<boolean> => {
    if (!jobId) return false;
    setError(null);
    try {
      const payload: JobControlRequest = { action, params };
      const response = await controlJob(apiPort, jobId, payload, workspaceId);
      if (response.debug) {
        setDebugState(response.debug);
      } else {
        await refresh();
      }
      onStatusChange(response.control_state);
      return true;
    } catch (cause: unknown) {
      const message = cause instanceof Error ? cause.message : String(cause);
      setError(message);
      onStatusChange(`调试动作失败: ${message}`);
      return false;
    }
  }, [apiPort, jobId, onStatusChange, refresh, workspaceId]);

  const addBreakpoint = () => {
    const normalizedToolName = toolName.trim();
    void sendAction("debug_set_breakpoint", {
      kind: breakpointKind,
      ...(normalizedToolName ? { tool_name: normalizedToolName } : {}),
    }).then((succeeded) => {
      if (succeeded) setToolName("");
    });
  };

  const traceTimeline = useMemo(
    () => traceEvents
      .filter((event) => event.job_id === jobId)
      .filter((event) => event.type.startsWith("debug_") || event.type === "tool_call_start" || event.type === "tool_call_end")
      .slice(-12)
      .reverse(),
    [jobId, traceEvents],
  );

  const activeStop = debugState?.active_stop ?? null;
  const breakpoints = debugState?.breakpoints ?? [];
  const actions = [...(debugState?.actions ?? [])].reverse().slice(0, 8);

  return (
    <aside className="debug-panel" aria-label="Agent 调试工作台">
      <NodeDebugPanel
        apiPort={apiPort}
        workspaceId={workspaceId}
        sessionId={sessionId}
        activeFilePath={activeFilePath}
        onOpenWorkspacePath={onOpenWorkspacePath}
        onStatusChange={onStatusChange}
      />
      <header className="debug-panel-header">
        <div>
          <strong>Agent 调试</strong>
          <span>{jobId ? `Job ${jobId}` : "选择一个运行中的会话"}</span>
        </div>
        <button type="button" className="debug-refresh-button" onClick={() => void refresh()} disabled={!jobId || loading}>
          {loading ? "加载中" : "刷新"}
        </button>
      </header>

      {!jobId ? (
        <div className="debug-empty-state">发送一条消息后，可以在这里设置工具前后和模型前断点。</div>
      ) : (
        <>
          <section className="debug-section debug-control-section" aria-label="调试控制权">
            <div className="debug-section-title-row">
              <h3>控制权</h3>
              <span className={activeStop ? "debug-status stopped" : "debug-status"}>
                {activeStop ? "已暂停" : "运行中"}
              </span>
            </div>
            <select
              className="debug-mode-select"
              value={debugState?.mode ?? "collaborative"}
              onChange={(event) => void sendAction("debug_set_mode", { mode: event.target.value })}
              aria-label="调试控制模式"
            >
              {MODE_OPTIONS.map((option) => <option key={option.value} value={option.value}>{option.label}</option>)}
            </select>
            <div className="debug-control-buttons">
              <button type="button" onClick={() => void sendAction("debug_continue")} disabled={!activeStop}>继续</button>
              <button type="button" onClick={() => void sendAction("debug_step_tool")} disabled={!activeStop}>单步工具</button>
              <button type="button" onClick={() => void sendAction("debug_takeover")} disabled={!activeStop}>人类接管</button>
              <button type="button" onClick={() => void sendAction("debug_handoff_to_ai")} disabled={!activeStop}>交给 AI</button>
              <button type="button" onClick={() => void sendAction("debug_explain_state")} disabled={!activeStop}>解释当前停止</button>
            </div>
          </section>

          <section className="debug-section" aria-label="断点列表">
            <div className="debug-section-title-row">
              <h3>断点</h3>
              <span>{breakpoints.length} 个</span>
            </div>
            <div className="debug-breakpoint-form">
              <select value={breakpointKind} onChange={(event) => setBreakpointKind(event.target.value as BreakpointKind)} aria-label="断点位置">
                {BREAKPOINT_OPTIONS.map((option) => <option key={option.value} value={option.value}>{option.label}</option>)}
              </select>
              <input
                value={toolName}
                onChange={(event) => setToolName(event.target.value)}
                placeholder="工具名（留空=全部）"
                aria-label="匹配工具名"
              />
              <button type="button" onClick={addBreakpoint}>添加</button>
            </div>
            <div className="debug-breakpoint-list">
              {breakpoints.length === 0 ? <span className="debug-muted">尚未设置断点</span> : null}
              {breakpoints.map((breakpoint) => (
                <div className="debug-breakpoint-row" key={breakpoint.breakpoint_id}>
                  <span className="debug-breakpoint-dot" aria-hidden="true" />
                  <span>{breakpointLabel(breakpoint.kind)}{breakpoint.tool_name ? ` · ${breakpoint.tool_name}` : " · 全部工具"}</span>
                  <button type="button" title="清除断点" aria-label={`清除${breakpoint.breakpoint_id}`} onClick={() => void sendAction("debug_clear_breakpoint", { breakpoint_id: breakpoint.breakpoint_id })}>×</button>
                </div>
              ))}
            </div>
            {breakpoints.length > 0 ? <button type="button" className="debug-clear-all" onClick={() => void sendAction("debug_clear_breakpoint")}>清除全部断点</button> : null}
          </section>

          <section className="debug-section" aria-label="停止快照">
            <div className="debug-section-title-row">
              <h3>停止快照</h3>
              {debugState?.stop_count ? <span>第 {debugState.stop_count} 次</span> : null}
            </div>
            {activeStop ? (
              <div className="debug-stop-card">
                <strong>{stopLabel(activeStop)}</strong>
                <p>{activeStop.explanation}</p>
                {Object.keys(activeStop.args ?? {}).length > 0 ? <details open><summary>输入参数</summary><pre>{displayJson(activeStop.args)}</pre></details> : null}
                {activeStop.result ? <details><summary>工具结果</summary><pre>{activeStop.result}</pre></details> : null}
              </div>
            ) : (
              <div className="debug-empty-state compact">未命中断点。运行时快照会出现在这里。</div>
            )}
          </section>

          <section className="debug-section" aria-label="调试时间线">
            <div className="debug-section-title-row"><h3>共同时间线</h3><span>最近事件</span></div>
            <div className="debug-timeline">
              {traceTimeline.length === 0 ? <span className="debug-muted">暂无调试事件</span> : null}
              {traceTimeline.map((event) => (
                <div className="debug-timeline-row" key={event.event_id}>
                  <span className={`debug-timeline-marker ${event.type.startsWith("debug_") ? "debug" : "tool"}`} />
                  <span>{event.type === "debug_stop" ? "系统命中断点" : event.type === "debug_action" ? "控制动作" : event.type === "tool_call_start" ? "AI 调用工具" : "工具返回"}</span>
                  <small>{event.content ?? event.title ?? event.type}</small>
                </div>
              ))}
            </div>
            {actions.length > 0 ? (
              <details className="debug-audit-details">
                <summary>调试动作审计（{debugState?.actions?.length ?? 0}）</summary>
                {actions.map((action) => <div className="debug-audit-row" key={action.action_id}><span>{action.actor === "human" ? "人类" : action.actor === "ai" ? "AI" : "系统"}</span><strong>{action.action}</strong><small>{action.message}</small></div>)}
              </details>
            ) : null}
          </section>
        </>
      )}
      {error ? <div className="debug-error" role="alert">{error}</div> : null}
    </aside>
  );
}
