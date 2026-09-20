import { useRef } from "react";
import type { MutableRefObject, Dispatch, SetStateAction } from "react";

import {
  copyNodeDebugConfiguration,
  getNodeDebugConfiguration,
  importNodeDebugConfiguration,
} from "../../api";
import type { NodeDebugController } from "../../hooks/useNodeDebugController";
import type {
  NodeDebugCapabilities,
  NodeDebugConfiguration,
  NodeDebugLaunchProfile,
  NodeDebugState,
  Session,
} from "../../types/backend";
import { nodeDebugProfileLabel } from "./nodeDebugPresentation";

interface NodeDebugConfigurationViewProps {
  apiPort: number;
  workspaceId: string | null;
  sessionId: string | null;
  threadId: string;
  activeFilePath: string | null;
  state: NodeDebugState | null;
  capabilities: NodeDebugCapabilities | null;
  sessions: Session[];
  extensionWindow: boolean;
  actionBusy: boolean;
  loading: boolean;
  configurationLocked: boolean;
  processCanRestart: boolean;
  activeProfile: NodeDebugLaunchProfile | null;
  launchProfiles: NodeDebugLaunchProfile[];
  configurationName: string;
  setConfigurationName: Dispatch<SetStateAction<string>>;
  scriptPath: string;
  setScriptPath: Dispatch<SetStateAction<string>>;
  workingDirectory: string;
  setWorkingDirectory: Dispatch<SetStateAction<string>>;
  scriptArgs: string;
  setScriptArgs: Dispatch<SetStateAction<string>>;
  newConfigurationName: string;
  setNewConfigurationName: Dispatch<SetStateAction<string>>;
  copyTargetSessionId: string;
  setCopyTargetSessionId: Dispatch<SetStateAction<string>>;
  scriptPathInputRef: MutableRefObject<HTMLInputElement | null>;
  setLocalNotice: Dispatch<SetStateAction<string | null>>;
  transferOwnerKey: string;
  transferOwnerKeyRef: MutableRefObject<string>;
  onStartDebugging: () => void;
  onStatusChange: (message: string) => void;
  onSetConfigurationView: () => void;
  refresh: NodeDebugController["refresh"];
  createConfiguration: NodeDebugController["createConfiguration"];
  updateConfiguration: NodeDebugController["updateConfiguration"];
  activateConfiguration: NodeDebugController["activateConfiguration"];
  deleteConfiguration: NodeDebugController["deleteConfiguration"];
}

export default function NodeDebugConfigurationView({
  apiPort,
  workspaceId,
  sessionId,
  threadId,
  activeFilePath,
  state,
  capabilities,
  sessions,
  extensionWindow,
  actionBusy,
  loading,
  configurationLocked,
  processCanRestart,
  activeProfile,
  launchProfiles,
  configurationName,
  setConfigurationName,
  scriptPath,
  setScriptPath,
  workingDirectory,
  setWorkingDirectory,
  scriptArgs,
  setScriptArgs,
  newConfigurationName,
  setNewConfigurationName,
  copyTargetSessionId,
  setCopyTargetSessionId,
  scriptPathInputRef,
  setLocalNotice,
  transferOwnerKey,
  transferOwnerKeyRef,
  onStartDebugging,
  onStatusChange,
  onSetConfigurationView,
  refresh,
  createConfiguration,
  updateConfiguration,
  activateConfiguration,
  deleteConfiguration,
}: NodeDebugConfigurationViewProps) {
  const importInputRef = useRef<HTMLInputElement | null>(null);
  const normalizedDraftArgs = scriptArgs.trim() ? scriptArgs.trim().split(/\s+/u) : [];

  const handleProfileChange = (name: string) => {
    setLocalNotice(null);
    setConfigurationName(name);
    const profile = launchProfiles.find((item) => item.name === name);
    if (!profile) return;
    if (profile.program) setScriptPath(profile.program);
    setWorkingDirectory(profile.working_directory ?? "");
    setScriptArgs((profile.args ?? []).join(" "));
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
      breakpoints: (state?.breakpoints ?? []).map((breakpoint) => ({
        path: breakpoint.path,
        line: breakpoint.line,
        column: breakpoint.column,
        condition: breakpoint.condition,
        hit_condition: breakpoint.hit_condition,
        log_message: breakpoint.log_message,
      })),
    });
  };

  const refreshAfterTransferFailure = async (
    operation: string,
    cause: unknown,
    mutationOwnerKey: string,
  ) => {
    const message = `${operation}失败: ${cause instanceof Error ? cause.message : String(cause)}`;
    if (transferOwnerKeyRef.current !== mutationOwnerKey) return;
    setLocalNotice(message);
    onStatusChange(message);
    try {
      await refresh();
    } catch (refreshCause: unknown) {
      const refreshMessage = `${message}；重新获取调试状态失败: ${refreshCause instanceof Error ? refreshCause.message : String(refreshCause)}`;
      if (transferOwnerKeyRef.current === mutationOwnerKey) {
        setLocalNotice(refreshMessage);
        onStatusChange(refreshMessage);
      }
    }
  };

  const exportActiveScheme = async () => {
    const configurationId = state?.active_configuration_id;
    if (!sessionId || !configurationId) return;
    const mutationOwnerKey = transferOwnerKey;
    setLocalNotice(null);
    try {
      const configuration = await getNodeDebugConfiguration(
        apiPort,
        sessionId,
        threadId,
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
      if (transferOwnerKeyRef.current === mutationOwnerKey) {
        onStatusChange(`已导出调试方案: ${configuration.name}`);
      }
    } catch (cause: unknown) {
      const message = `导出调试方案失败: ${cause instanceof Error ? cause.message : String(cause)}`;
      if (transferOwnerKeyRef.current === mutationOwnerKey) {
        setLocalNotice(message);
        onStatusChange(message);
      }
    }
  };

  const importScheme = async (file: File) => {
    if (!sessionId) return;
    const mutationOwnerKey = transferOwnerKey;
    setLocalNotice(null);
    try {
      const configuration = JSON.parse(await file.text()) as NodeDebugConfiguration;
      const nextState = await importNodeDebugConfiguration(
        apiPort,
        {
          session_id: sessionId,
          thread_id: threadId,
          configuration,
          activate: false,
        },
        workspaceId,
      );
      if (transferOwnerKeyRef.current !== mutationOwnerKey) return;
      onStatusChange(`已导入调试方案: ${configuration.name}`);
      try {
        await refresh();
      } catch (refreshCause: unknown) {
        const refreshMessage = `已导入调试方案: ${configuration.name}；重新获取调试状态失败: ${refreshCause instanceof Error ? refreshCause.message : String(refreshCause)}`;
        if (transferOwnerKeyRef.current === mutationOwnerKey) {
          setLocalNotice(refreshMessage);
          onStatusChange(refreshMessage);
        }
      }
      if (transferOwnerKeyRef.current === mutationOwnerKey && !nextState.active_configuration_id) {
        onSetConfigurationView();
      }
    } catch (cause: unknown) {
      await refreshAfterTransferFailure("导入调试方案", cause, mutationOwnerKey);
    }
  };

  const copyActiveScheme = async () => {
    const configurationId = state?.active_configuration_id;
    if (!sessionId || !configurationId || !copyTargetSessionId) return;
    const mutationOwnerKey = transferOwnerKey;
    setLocalNotice(null);
    try {
      const copied = await copyNodeDebugConfiguration(
        apiPort,
        configurationId,
        {
          source_session_id: sessionId,
          source_thread_id: threadId,
          target_session_id: copyTargetSessionId,
          target_thread_id: "main",
          activate: false,
        },
        workspaceId,
      );
      if (transferOwnerKeyRef.current === mutationOwnerKey) {
        onStatusChange(`已复制调试方案到另一会话: ${copied.name}`);
      }
    } catch (cause: unknown) {
      await refreshAfterTransferFailure("复制调试方案", cause, mutationOwnerKey);
    }
  };

  return (
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
                disabled={actionBusy || configurationLocked || configuration.configuration_id === state?.active_configuration_id}
                title={configurationLocked ? "调试实例尚未结清，结清后才能切换" : `切换到 ${configuration.name}`}
              >
                <strong>{configuration.name}</strong>
                <small>{configuration.script_path ?? "尚未选择入口"} · {configuration.breakpoint_count} 个断点</small>
              </button>
              <button
                type="button"
                className="danger"
                onClick={() => void deleteConfiguration(configuration.configuration_id)}
                disabled={actionBusy || (configurationLocked && configuration.configuration_id === state?.active_configuration_id)}
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
          <button type="button" onClick={createScheme} disabled={!sessionId || actionBusy || configurationLocked}>新建方案</button>
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
          <input ref={scriptPathInputRef} value={scriptPath} onChange={(event) => { setLocalNotice(null); setScriptPath(event.target.value); }} placeholder="选择当前编辑器文件或输入工作区相对路径" />
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
      <button type="button" className="node-debug-start-button" onClick={onStartDebugging} disabled={!sessionId || loading || actionBusy || state?.status === "stopping" || state?.status === "reconcile_required" || capabilities?.enabled === false || activeProfile?.supported === false}>
        <span className="codicon codicon-debug-start" aria-hidden="true" />
        {processCanRestart ? "重启调试" : state?.status === "stopping" ? "停止中" : state?.status === "reconcile_required" ? "需先核实旧实例" : "启动调试"}
      </button>
      <button type="button" onClick={saveActiveScheme} disabled={!state?.active_configuration_id || actionBusy || configurationLocked}>
        保存当前方案
      </button>
      {activeProfile?.supported === false ? <div className="debug-error">当前版本未实现 {activeProfile.adapter}；不会回退成 Node 调试。</div> : null}
      {capabilities?.enabled === false ? <div className="debug-error">当前工作区已关闭源码调试能力。</div> : null}
    </div>
  );
}
