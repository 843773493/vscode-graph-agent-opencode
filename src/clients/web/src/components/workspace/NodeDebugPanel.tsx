import { useEffect, useMemo, useRef, useState } from "react";

import {
  copyNodeDebugConfiguration,
  getNodeDebugConfiguration,
  importNodeDebugConfiguration,
} from "../../api";
import type { NodeDebugController } from "../../hooks/useNodeDebugController";
import type {
  NodeDebugVariable,
  Session,
} from "../../types/backend";
import NodeDebugSourcePreview from "./NodeDebugSourcePreview";
import {
  nodeDebugBreakpointLabel,
  type NodeDebugBreakpointDefinition,
} from "./NodeDebugBreakpointGutter";
import {
  nodeDebugActionActor,
  nodeDebugPauseReasonLabel,
  nodeDebugProfileLabel,
  nodeDebugStatusLabel,
} from "./nodeDebugPresentation";
import { resolveNodeDebugSourceSelection } from "./nodeDebugViewState";

interface NodeDebugPanelProps {
  apiPort: number;
  workspaceId: string | null;
  sessionId: string | null;
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
  const importInputRef = useRef<HTMLInputElement | null>(null);
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
  const processCanStop = status === "starting" || status === "running" || status === "paused";
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
    setBreakpointLine("");
    setBreakpointCondition("");
    setExpression("");
    setLocalNotice(null);
    setSelectedSourceLocation(null);
    followedFrameRef.current = null;
    followedBreakpointRef.current = null;
  }, [sessionId, workspaceId]);

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

  const handleProfileChange = (name: string) => {
    setLocalNotice(null);
    setConfigurationName(name);
    const profile = launchProfiles.find((item) => item.name === name);
    if (!profile) return;
    if (profile.program) setScriptPath(profile.program);
    setWorkingDirectory(profile.working_directory ?? "");
    setScriptArgs((profile.args ?? []).join(" "));
  };

  const startDebugging = () => {
    setLocalNotice(null);
    const path = scriptPath.trim();
    if (!path) {
      const message = "启动源码调试失败：请选择或填写 JavaScript 文件";
      setLocalNotice(message);
      onStatusChange(message);
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

  const createScheme = () => {
    const name = newConfigurationName.trim();
    if (!name) {
      onStatusChange("创建调试方案失败：请输入方案名称");
      return;
    }
    void createConfiguration({
      name,
      path: scriptPath.trim() || null,
      workingDirectory: workingDirectory.trim(),
      launchProfileName: configurationName || null,
      args: normalizedDraftArgs,
    }).then((nextState) => {
      if (nextState) setNewConfigurationName("");
    });
  };

  const saveActiveScheme = () => {
    const configurationId = state?.active_configuration_id;
    const name = state?.active_configuration_name;
    if (!configurationId || !name) {
      onStatusChange("保存调试方案失败：当前没有活动方案");
      return;
    }
    setLocalNotice(null);
    void updateConfiguration({
      configurationId,
      name,
      path: scriptPath.trim() || null,
      workingDirectory: workingDirectory.trim(),
      launchProfileName: configurationName || null,
      args: normalizedDraftArgs,
      breakpoints: breakpoints.map((breakpoint) => ({
        path: breakpoint.path,
        line: breakpoint.line,
        column: breakpoint.column,
        condition: breakpoint.condition,
        hit_condition: breakpoint.hit_condition,
        log_message: breakpoint.log_message,
      })),
    });
  };

  const exportActiveScheme = async () => {
    const configurationId = state?.active_configuration_id;
    if (!sessionId || !configurationId) return;
    try {
      const configuration = await getNodeDebugConfiguration(
        apiPort,
        sessionId,
        configurationId,
        workspaceId,
      );
      const blob = new Blob([JSON.stringify(configuration, null, 2)], { type: "application/json" });
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = `${configuration.configuration_id}.json`;
      anchor.click();
      URL.revokeObjectURL(url);
      onStatusChange(`已导出调试方案: ${configuration.name}`);
    } catch (cause: unknown) {
      onStatusChange(`导出调试方案失败: ${cause instanceof Error ? cause.message : String(cause)}`);
    }
  };

  const importScheme = async (file: File) => {
    if (!sessionId) return;
    try {
      const configuration = JSON.parse(await file.text()) as Parameters<typeof importNodeDebugConfiguration>[2];
      const nextState = await importNodeDebugConfiguration(
        apiPort,
        sessionId,
        configuration,
        workspaceId,
      );
      onStatusChange(`已导入调试方案: ${configuration.name}`);
      await refresh();
      if (!nextState.active_configuration_id) setView("configuration");
    } catch (cause: unknown) {
      onStatusChange(`导入调试方案失败: ${cause instanceof Error ? cause.message : String(cause)}`);
    }
  };

  const copyActiveScheme = async () => {
    const configurationId = state?.active_configuration_id;
    if (!sessionId || !configurationId || !copyTargetSessionId) return;
    try {
      const copied = await copyNodeDebugConfiguration(
        apiPort,
        sessionId,
        copyTargetSessionId,
        configurationId,
        workspaceId,
      );
      onStatusChange(`已复制调试方案到另一会话: ${copied.name}`);
    } catch (cause: unknown) {
      onStatusChange(`复制调试方案失败: ${cause instanceof Error ? cause.message : String(cause)}`);
    }
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
          <button type="button" className="primary" onClick={startDebugging} disabled={!sessionId || loading || capabilities?.enabled === false}>
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
        <div className="node-debug-view node-debug-configuration-view" role="tabpanel">
          <section className="node-debug-section node-debug-scheme-section">
            <div className="debug-section-title-row">
              <h3>会话调试方案</h3>
              <span>{state?.configurations?.length ?? 0} 套</span>
            </div>
            <div className="node-debug-scheme-list">
              {(state?.configurations ?? []).map((configuration) => (
                <div className={configuration.configuration_id === state?.active_configuration_id ? "active" : ""} key={configuration.configuration_id}>
                  <button
                    type="button"
                    onClick={() => void activateConfiguration(configuration.configuration_id)}
                    disabled={actionBusy || processCanStop || configuration.configuration_id === state?.active_configuration_id}
                    title={processCanStop ? "目标程序运行中，停止后才能切换" : `切换到 ${configuration.name}`}
                  >
                    <strong>{configuration.name}</strong>
                    <small>{configuration.script_path ?? "尚未选择入口"} · {configuration.breakpoint_count} 个断点</small>
                  </button>
                  <button
                    type="button"
                    className="danger"
                    onClick={() => void deleteConfiguration(configuration.configuration_id)}
                    disabled={actionBusy || (processCanStop && configuration.configuration_id === state?.active_configuration_id)}
                    aria-label={`删除调试方案 ${configuration.name}`}
                  >
                    <span className="codicon codicon-trash" aria-hidden="true" />
                  </button>
                </div>
              ))}
              {(state?.configurations?.length ?? 0) === 0 ? <span className="debug-muted">尚无方案；模型首次设置断点或启动时也会自动创建。</span> : null}
            </div>
            <div className="node-debug-scheme-create">
              <input value={newConfigurationName} onChange={(event) => setNewConfigurationName(event.target.value)} placeholder="新方案名称" />
              <button type="button" onClick={createScheme} disabled={!sessionId || actionBusy || processCanStop}>新建方案</button>
            </div>
            <p className="debug-muted">单个方案 JSON 可复制到另一会话；导入导出和跨会话复制放在扩展窗口的方案菜单。</p>
            {extensionWindow ? (
              <details className="node-debug-scheme-transfer">
                <summary>迁移方案</summary>
                <div>
                  <button type="button" onClick={() => void exportActiveScheme()} disabled={!state?.active_configuration_id}>导出 JSON</button>
                  <button type="button" onClick={() => importInputRef.current?.click()}>导入 JSON</button>
                  <input
                    ref={importInputRef}
                    type="file"
                    accept="application/json,.json"
                    hidden
                    onChange={(event) => {
                      const file = event.target.files?.[0];
                      if (file) void importScheme(file);
                      event.target.value = "";
                    }}
                  />
                </div>
                <div>
                  <select value={copyTargetSessionId} onChange={(event) => setCopyTargetSessionId(event.target.value)}>
                    <option value="">复制到会话…</option>
                    {sessions.filter((session) => session.session_id !== sessionId).map((session) => (
                      <option value={session.session_id} key={session.session_id}>{session.title}</option>
                    ))}
                  </select>
                  <button type="button" onClick={() => void copyActiveScheme()} disabled={!state?.active_configuration_id || !copyTargetSessionId}>复制</button>
                </div>
              </details>
            ) : null}
          </section>
          <label>
            工作区启动 Profile
            <select value={configurationName} onChange={(event) => handleProfileChange(event.target.value)}>
              {launchProfiles.map((profile) => (
                <option value={profile.name} key={profile.name}>{nodeDebugProfileLabel(profile)}</option>
              ))}
            </select>
          </label>
          <label>
            JavaScript 文件
            <div className="node-debug-path-field">
              <input value={scriptPath} onChange={(event) => { setLocalNotice(null); setScriptPath(event.target.value); }} placeholder="选择当前编辑器文件或输入工作区相对路径" />
              {activeFilePath ? <button type="button" onClick={() => { setLocalNotice(null); setScriptPath(activeFilePath); }}>当前文件</button> : null}
            </div>
          </label>
          <label>
            工作目录
            <input value={workingDirectory} onChange={(event) => { setLocalNotice(null); setWorkingDirectory(event.target.value); }} placeholder="留空使用工作区根目录" />
          </label>
          <label>
            参数
            <input value={scriptArgs} onChange={(event) => { setLocalNotice(null); setScriptArgs(event.target.value); }} placeholder="以空格分隔" />
          </label>
          <button type="button" className="node-debug-start-button" onClick={startDebugging} disabled={!sessionId || loading || capabilities?.enabled === false || activeProfile?.supported === false}>
            <span className="codicon codicon-debug-start" aria-hidden="true" />
            {processCanStop ? "重启调试" : "启动调试"}
          </button>
          <button type="button" onClick={saveActiveScheme} disabled={!state?.active_configuration_id || actionBusy || processCanStop}>
            保存当前方案
          </button>
          {activeProfile?.supported === false ? <div className="debug-error">当前版本未实现 {activeProfile.adapter}；不会回退成 Node 调试。</div> : null}
          {capabilities?.enabled === false ? <div className="debug-error">当前工作区已关闭源码调试能力。</div> : null}
        </div>
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
