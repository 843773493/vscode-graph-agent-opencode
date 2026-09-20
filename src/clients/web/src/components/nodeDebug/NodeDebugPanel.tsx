import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";

import type { NodeDebugController } from "../../hooks/useNodeDebugController";
import type {
  NodeDebugVariable,
  Session,
} from "../../types/backend";
import NodeDebugSourcePreview from "./NodeDebugSourcePreview";
import NodeDebugConfigurationView from "./NodeDebugConfigurationView";
import {
  nodeDebugBreakpointLabel,
  type NodeDebugBreakpointDefinition,
} from "./NodeDebugBreakpointGutter";
import {
  nodeDebugActionActor,
  nodeDebugPauseReasonLabel,
  nodeDebugStatusLabel,
} from "./nodeDebugPresentation";
import { resolveNodeDebugSourceSelection } from "./nodeDebugViewState";

interface NodeDebugPanelProps {
  apiPort: number;
  workspaceId: string | null;
  sessionId: string | null;
  threadId: string;
  activeFilePath: string | null;
  controller: NodeDebugController;
  sessions: Session[];
  compact?: boolean;
  extensionWindow?: boolean;
  onOpenExtensionWindow?: () => void;
  onOpenWorkspacePath: (path: string) => Promise<void>;
  onStatusChange: (message: string) => void;
}

type NodeDebugView = "source" | "context" | "console" | "configuration";

const GLOBAL_VARIABLE_PREVIEW_LIMIT = 80;

const DEBUG_VIEWS: ReadonlyArray<{
  id: NodeDebugView;
  label: string;
  icon: string;
}> = [
  { id: "source", label: "源码", icon: "codicon-code" },
  { id: "context", label: "上下文", icon: "codicon-list-tree" },
  { id: "console", label: "控制台", icon: "codicon-terminal" },
  { id: "configuration", label: "配置", icon: "codicon-settings-gear" },
];

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

export default function NodeDebugPanel({
  apiPort,
  workspaceId,
  sessionId,
  threadId,
  activeFilePath,
  controller,
  sessions,
  compact = false,
  extensionWindow = false,
  onOpenExtensionWindow,
  onOpenWorkspacePath,
  onStatusChange,
}: NodeDebugPanelProps) {
  const {
    state,
    capabilities,
    error,
    loading,
    actionBusy,
    refresh,
    runAction,
    start,
    createConfiguration,
    updateConfiguration,
    activateConfiguration,
    deleteConfiguration,
  } = controller;
  const [view, setView] = useState<NodeDebugView>("source");
  const [scriptPath, setScriptPath] = useState("");
  const [workingDirectory, setWorkingDirectory] = useState("");
  const [scriptArgs, setScriptArgs] = useState("");
  const [configurationName, setConfigurationName] = useState("");
  const [newConfigurationName, setNewConfigurationName] = useState("");
  const [copyTargetSessionId, setCopyTargetSessionId] = useState("");
  const scriptPathInputRef = useRef<HTMLInputElement | null>(null);
  const [breakpointLine, setBreakpointLine] = useState("");
  const [breakpointCondition, setBreakpointCondition] = useState("");
  const [expression, setExpression] = useState("");
  const [localNotice, setLocalNotice] = useState<string | null>(null);
  const [selectedSourceLocation, setSelectedSourceLocation] = useState<{
    path: string;
    line: number;
  } | null>(null);
  const followedFrameRef = useRef<string | null>(null);
  const followedBreakpointRef = useRef<string | null>(null);
  const transferOwnerKey = `${workspaceId ?? ""}:${sessionId ?? ""}:${threadId}`;
  const transferOwnerKeyRef = useRef(transferOwnerKey);

  useLayoutEffect(() => {
    transferOwnerKeyRef.current = transferOwnerKey;
  }, [transferOwnerKey]);

  const status = state?.status ?? "idle";
  const activeFrame = state?.call_stack?.[0] ?? null;
  const localVariables = useMemo(
    () => (activeFrame?.variables ?? []).filter((variable) => variable.scope !== "global"),
    [activeFrame?.variables],
  );
  const globalVariables = useMemo(
    () => (activeFrame?.variables ?? []).filter((variable) => variable.scope === "global"),
    [activeFrame?.variables],
  );
  const breakpoints = state?.breakpoints ?? [];
  const sourceSelection = resolveNodeDebugSourceSelection({
    state,
    selectedPath: selectedSourceLocation?.path ?? null,
    selectedLine: selectedSourceLocation?.line ?? null,
    draftScriptPath: scriptPath,
  });
  const sourcePath = sourceSelection.path;
  const sourceFocusLine = sourceSelection.focusLine;
  const launchProfiles = capabilities?.launch_profiles ?? [];
  const activeProfile = launchProfiles.find(
    (profile) => profile.name === configurationName,
  ) ?? null;
  const processCanRestart = status === "starting" || status === "running" || status === "paused";
  const processCanStop = processCanRestart || status === "reconcile_required";
  const configurationLocked = processCanStop || status === "stopping";
  const normalizedDraftArgs = useMemo(
    () => (scriptArgs.trim() ? scriptArgs.trim().split(/\s+/u) : []),
    [scriptArgs],
  );
  const currentArgs = state?.args ?? [];
  const hasUnsavedConfigurationChanges = Boolean(
    state?.active_configuration_id
      && (
        scriptPath.trim() !== (state.script_path ?? "")
        || workingDirectory.trim() !== (state.working_directory ?? "")
        || (configurationName || null) !== (state.launch_profile_name ?? null)
        || normalizedDraftArgs.length !== currentArgs.length
        || normalizedDraftArgs.some((argument, index) => argument !== currentArgs[index])
      ),
  );
  const recentActions = useMemo(
    () => [...(state?.actions ?? [])].reverse().slice(0, extensionWindow ? 40 : 12),
    [extensionWindow, state?.actions],
  );

  useEffect(() => {
    setScriptPath("");
    setWorkingDirectory("");
    setScriptArgs("");
    setConfigurationName("");
    setNewConfigurationName("");
    setCopyTargetSessionId("");
    setBreakpointLine("");
    setBreakpointCondition("");
    setExpression("");
    setLocalNotice(null);
    setSelectedSourceLocation(null);
    followedFrameRef.current = null;
    followedBreakpointRef.current = null;
  }, [sessionId, threadId, workspaceId]);

  useEffect(() => {
    if (!state) return;
    setScriptPath(state.script_path ?? "");
    setWorkingDirectory(state.working_directory ?? "");
    setScriptArgs((state.args ?? []).join(" "));
    setConfigurationName(state.launch_profile_name ?? "");
    setSelectedSourceLocation(null);
    followedBreakpointRef.current = null;
  }, [state?.active_configuration_id]);

  useEffect(() => {
    if (!state) return;
    setScriptPath(state.script_path ?? "");
    setWorkingDirectory(state.working_directory ?? "");
    setScriptArgs((state.args ?? []).join(" "));
    setConfigurationName(state.launch_profile_name ?? "");
  }, [state?.configuration_revision]);

  useEffect(() => {
    if (configurationName || !capabilities || state?.active_configuration_id) return;
    const preferred = launchProfiles.find((profile) => profile.supported);
    if (preferred) setConfigurationName(preferred.name);
  }, [capabilities, configurationName, launchProfiles, state?.active_configuration_id]);

  useEffect(() => {
    if (!extensionWindow || !activeFrame?.path) return;
    const followKey = `${activeFrame.path}:${activeFrame.line}`;
    if (followedFrameRef.current === followKey) return;
    followedFrameRef.current = followKey;
    void onOpenWorkspacePath(activeFrame.path);
  }, [activeFrame?.line, activeFrame?.path, extensionWindow, onOpenWorkspacePath]);

  useEffect(() => {
    if (activeFrame?.path || breakpoints.length === 0) return;
    const breakpoint = breakpoints[breakpoints.length - 1];
    const followKey = `${breakpoint.breakpoint_id}:${breakpoint.path}:${breakpoint.line}`;
    if (followedBreakpointRef.current === followKey) return;
    followedBreakpointRef.current = followKey;
    setSelectedSourceLocation({ path: breakpoint.path, line: breakpoint.line });
  }, [activeFrame?.path, breakpoints]);

  const startDebugging = () => {
    setLocalNotice(null);
    const path = scriptPath.trim();
    if (!path) {
      const message = "请先在配置页选择或填写 JavaScript 文件，再启动源码调试";
      setLocalNotice(message);
      onStatusChange(message);
      setView("configuration");
      window.requestAnimationFrame(() => scriptPathInputRef.current?.focus());
      return;
    }
    if (activeProfile && !activeProfile.supported) {
      const message = `启动源码调试失败：当前版本不支持 ${activeProfile.adapter}`;
      setLocalNotice(message);
      onStatusChange(message);
      return;
    }
    if (hasUnsavedConfigurationChanges) {
      const message = "当前方案有未保存修改，请先保存方案再启动";
      setLocalNotice(message);
      onStatusChange(message);
      setView("configuration");
      return;
    }
    void start({
      path,
      workingDirectory: workingDirectory.trim() || null,
      launchProfileName: configurationName || null,
      configurationId: state?.active_configuration_id ?? null,
      args: normalizedDraftArgs,
    });
  };

  const changeBreakpoint = (
    path: string,
    line: number,
    breakpointId: string | null,
    definition: NodeDebugBreakpointDefinition | null,
  ) => {
    setSelectedSourceLocation({ path, line });
    if (!definition) {
      if (!breakpointId) return;
      void runAction("clear_breakpoint", { breakpoint_id: breakpointId });
      return;
    }
    void runAction(breakpointId ? "update_breakpoint" : "set_breakpoint", {
      ...(breakpointId ? { breakpoint_id: breakpointId } : {}),
      path,
      line,
      condition: definition.condition,
      hit_condition: definition.hit_condition,
      log_message: definition.log_message,
    });
  };

  const addBreakpointFromForm = () => {
    const line = Number(breakpointLine);
    const path = (sourcePath ?? scriptPath).trim();
    if (!path || !Number.isSafeInteger(line) || line < 1) {
      onStatusChange("设置源码断点失败：需要有效文件和正整数行号");
      return;
    }
    void runAction("set_breakpoint", {
      path,
      line,
      ...(breakpointCondition.trim() ? { condition: breakpointCondition.trim() } : {}),
    });
  };

  const evaluate = () => {
    const normalized = expression.trim();
    if (!normalized) return;
    void runAction("evaluate", { expression: normalized });
  };

  return (
    <section
      className={`node-debug-panel${compact ? " compact" : ""}${extensionWindow ? " extension-window" : ""}`}
      aria-label="Node 源码调试"
    >
      <header className="node-debug-header">
        <div>
          <strong>源码调试</strong>
          <span className={`node-debug-status ${status}`}>{nodeDebugStatusLabel(status)}</span>
          {state?.active_configuration_name ? <small>{state.active_configuration_name}</small> : null}
          {state?.paused_reason ? <small>{nodeDebugPauseReasonLabel(state.paused_reason)}</small> : null}
        </div>
        <div className="node-debug-header-actions">
          {!extensionWindow && onOpenExtensionWindow ? (
            <button type="button" onClick={onOpenExtensionWindow} title="在扩展窗口打开完整调试工作台">
              <span className="codicon codicon-open-preview" aria-hidden="true" />
              扩展窗口
            </button>
          ) : null}
          <button type="button" onClick={() => void refresh()} disabled={!sessionId} title="刷新调试状态">
            <span className="codicon codicon-refresh" aria-hidden="true" />
          </button>
        </div>
      </header>

      <div className="node-debug-controls" aria-label="源码调试控制">
        {status === "idle" || status === "exited" || status === "failed" ? (
          <button type="button" className="primary" onClick={startDebugging} disabled={!sessionId || loading || actionBusy || capabilities?.enabled === false}>
            <span className="codicon codicon-debug-start" aria-hidden="true" />
            {loading ? "启动中" : "启动"}
          </button>
        ) : (
          <button type="button" onClick={() => void runAction("continue")} disabled={actionBusy || status !== "paused"} title="继续">
            <span className="codicon codicon-debug-continue" aria-hidden="true" />
          </button>
        )}
        <button type="button" onClick={() => void runAction("pause")} disabled={actionBusy || status !== "running"} title="暂停">
          <span className="codicon codicon-debug-pause" aria-hidden="true" />
        </button>
        <button type="button" onClick={() => void runAction("step_over")} disabled={actionBusy || status !== "paused"} title="单步跳过">
          <span className="codicon codicon-debug-step-over" aria-hidden="true" />
        </button>
        <button type="button" onClick={() => void runAction("step_into")} disabled={actionBusy || status !== "paused"} title="单步进入">
          <span className="codicon codicon-debug-step-into" aria-hidden="true" />
        </button>
        <button type="button" onClick={() => void runAction("step_out")} disabled={actionBusy || status !== "paused"} title="单步跳出">
          <span className="codicon codicon-debug-step-out" aria-hidden="true" />
        </button>
        <button type="button" onClick={() => void runAction("stop")} disabled={actionBusy || !processCanStop} title="停止">
          <span className="codicon codicon-debug-stop" aria-hidden="true" />
        </button>
      </div>
      {localNotice ? <div className="debug-error" role="alert">{localNotice}</div> : null}

      <nav className="node-debug-view-tabs" role="tablist" aria-label="源码调试二级菜单">
        {DEBUG_VIEWS.map((item) => (
          <button
            type="button"
            role="tab"
            aria-selected={view === item.id}
            className={view === item.id ? "active" : ""}
            onClick={() => setView(item.id)}
            key={item.id}
          >
            <span className={`codicon ${item.icon}`} aria-hidden="true" />
            {item.label}
          </button>
        ))}
      </nav>

      {view === "source" ? (
        <div className="node-debug-view node-debug-source-view" role="tabpanel">
          {!sourcePath ? (
            <div className="debug-empty-state compact" role="status">
              <span>尚未选择 JavaScript 入口。</span>
              <button type="button" onClick={() => setView("configuration")}>
                配置调试入口
              </button>
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
            onChangeBreakpoint={changeBreakpoint}
            onOpenWorkspacePath={onOpenWorkspacePath}
          />
          {status === "paused" && activeFrame ? (
            <div className="debug-empty-state compact" role="status">
              <span>已暂停在 {activeFrame.path ?? activeFrame.url}:{activeFrame.line}，{localVariables.length} 个局部变量。</span>
              <button type="button" onClick={() => setView("context")}>查看调用栈与变量</button>
            </div>
          ) : null}
          {status === "exited" && (state?.output?.length ?? 0) > 0 ? (
            <div className="debug-empty-state compact" role="status">
              <span>调试已结束，保留 {state?.output?.length ?? 0} 行程序输出。</span>
              <button type="button" onClick={() => setView("console")}>查看控制台</button>
            </div>
          ) : null}
          <details className="node-debug-secondary" open={extensionWindow}>
            <summary>断点列表与高级设置 <span>{breakpoints.length}</span></summary>
            <div className="node-debug-breakpoint-form">
              <input value={breakpointLine} onChange={(event) => setBreakpointLine(event.target.value)} inputMode="numeric" placeholder="行" aria-label="源码断点行号" />
              <input value={breakpointCondition} onChange={(event) => setBreakpointCondition(event.target.value)} placeholder="条件（可选）" aria-label="源码断点条件" />
              <button type="button" onClick={addBreakpointFromForm} disabled={!sessionId || actionBusy}>添加</button>
            </div>
            <div className="node-debug-breakpoint-list">
              {breakpoints.length === 0 ? <span className="debug-muted">尚未设置源码断点</span> : null}
              {breakpoints.map((breakpoint) => (
                <div className="node-debug-breakpoint-row" key={breakpoint.breakpoint_id}>
                  <span className={breakpoint.relocation_status === "pending_update" || breakpoint.relocation_status === "source_deleted" ? "stale" : breakpoint.verified ? "verified" : "unverified"} aria-hidden="true" />
                  <button
                    type="button"
                    onClick={() => {
                      setSelectedSourceLocation({ path: breakpoint.path, line: breakpoint.line });
                    }}
                    title={breakpoint.relocation_message ?? breakpoint.path}
                  >
                    {breakpoint.path}:{breakpoint.line}
                    {` · ${nodeDebugBreakpointLabel(breakpoint)}`}
                    {breakpoint.relocation_status === "relocated" ? " · 已重定位" : ""}
                    {breakpoint.relocation_status === "pending_update" ? " · 待更新" : ""}
                    {breakpoint.relocation_status === "source_deleted" ? " · 文件已删除" : ""}
                  </button>
                  <button type="button" onClick={() => void runAction("clear_breakpoint", { breakpoint_id: breakpoint.breakpoint_id })} aria-label="清除断点">
                    <span className="codicon codicon-close" aria-hidden="true" />
                  </button>
                </div>
              ))}
            </div>
          </details>
        </div>
      ) : null}

      {view === "context" ? (
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
              <input value={expression} onChange={(event) => setExpression(event.target.value)} onKeyDown={(event) => { if (event.key === "Enter") evaluate(); }} placeholder="输入表达式" />
              <button type="button" onClick={evaluate} disabled={actionBusy || status !== "paused"}>求值</button>
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
      ) : null}

      {view === "console" ? (
        <div className="node-debug-view node-debug-console-view" role="tabpanel">
          <section className="node-debug-section">
            <div className="debug-section-title-row"><h3>表达式控制台</h3><span>{status === "paused" ? "可用" : "需暂停"}</span></div>
            <div className="node-debug-evaluate-row">
              <input
                value={expression}
                onChange={(event) => setExpression(event.target.value)}
                onKeyDown={(event) => { if (event.key === "Enter") evaluate(); }}
                placeholder="例如 counter += 1"
                aria-label="调试控制台表达式"
              />
              <button type="button" onClick={evaluate} disabled={actionBusy || status !== "paused"}>求值</button>
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
      ) : null}

      {view === "configuration" ? (
        <NodeDebugConfigurationView
          apiPort={apiPort}
          workspaceId={workspaceId}
          sessionId={sessionId}
          threadId={threadId}
          activeFilePath={activeFilePath}
          state={state}
          capabilities={capabilities}
          sessions={sessions}
          extensionWindow={extensionWindow}
          actionBusy={actionBusy}
          loading={loading}
          configurationLocked={configurationLocked}
          processCanRestart={processCanRestart}
          activeProfile={activeProfile}
          launchProfiles={launchProfiles}
          configurationName={configurationName}
          setConfigurationName={setConfigurationName}
          scriptPath={scriptPath}
          setScriptPath={setScriptPath}
          workingDirectory={workingDirectory}
          setWorkingDirectory={setWorkingDirectory}
          scriptArgs={scriptArgs}
          setScriptArgs={setScriptArgs}
          newConfigurationName={newConfigurationName}
          setNewConfigurationName={setNewConfigurationName}
          copyTargetSessionId={copyTargetSessionId}
          setCopyTargetSessionId={setCopyTargetSessionId}
          scriptPathInputRef={scriptPathInputRef}
          setLocalNotice={setLocalNotice}
          transferOwnerKey={transferOwnerKey}
          transferOwnerKeyRef={transferOwnerKeyRef}
          onStartDebugging={startDebugging}
          onStatusChange={onStatusChange}
          onSetConfigurationView={() => setView("configuration")}
          refresh={refresh}
          createConfiguration={createConfiguration}
          updateConfiguration={updateConfiguration}
          activateConfiguration={activateConfiguration}
          deleteConfiguration={deleteConfiguration}
        />
      ) : null}

      {state?.error_message ? <div className="debug-error" role="alert">{state.error_message}</div> : null}
      {state?.requires_restart ? (
        <div className="debug-warning" role="status">
            源码已变化，相关断点已失效；当前进程仍可继续。需要运行新源码时再重启并重新设置断点：{(state.source_changed_paths ?? []).join("、")}
        </div>
      ) : null}
      {error ? <div className="debug-error" role="alert">{error}</div> : null}
    </section>
  );
}
