import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";

import type { NodeDebugController } from "../../hooks/useNodeDebugController";
import type { Session } from "../../types/backend";
import NodeDebugConfigurationView from "./NodeDebugConfigurationView";
import NodeDebugConsoleView from "./NodeDebugConsoleView";
import NodeDebugContextView from "./NodeDebugContextView";
import type { NodeDebugBreakpointDefinition } from "./NodeDebugBreakpointGutter";
import NodeDebugSourceView from "./NodeDebugSourceView";
import {
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
        <NodeDebugSourceView
          apiPort={apiPort}
          workspaceId={workspaceId}
          sessionId={sessionId}
          state={state}
          status={status}
          sourcePath={sourcePath}
          sourceFocusLine={sourceFocusLine}
          breakpoints={breakpoints}
          actionBusy={actionBusy}
          extensionWindow={extensionWindow}
          breakpointLine={breakpointLine}
          setBreakpointLine={setBreakpointLine}
          breakpointCondition={breakpointCondition}
          setBreakpointCondition={setBreakpointCondition}
          onChangeBreakpoint={changeBreakpoint}
          onAddBreakpoint={addBreakpointFromForm}
          onClearBreakpoint={(breakpointId) => void runAction("clear_breakpoint", { breakpoint_id: breakpointId })}
          onSelectSource={(path, line) => setSelectedSourceLocation({ path, line })}
          onShowConfiguration={() => setView("configuration")}
          onShowContext={() => setView("context")}
          onShowConsole={() => setView("console")}
          onOpenWorkspacePath={onOpenWorkspacePath}
        />
      ) : null}

      {view === "context" ? (
        <NodeDebugContextView
          state={state}
          status={status}
          actionBusy={actionBusy}
          expression={expression}
          setExpression={setExpression}
          onEvaluate={evaluate}
          onOpenWorkspacePath={onOpenWorkspacePath}
        />
      ) : null}

      {view === "console" ? (
        <NodeDebugConsoleView
          state={state}
          status={status}
          actionBusy={actionBusy}
          extensionWindow={extensionWindow}
          expression={expression}
          setExpression={setExpression}
          onEvaluate={evaluate}
        />
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
