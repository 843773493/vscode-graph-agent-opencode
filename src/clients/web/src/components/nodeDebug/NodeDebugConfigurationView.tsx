import { t, formatNumber } from "../../../../shared/core/i18n/index.js";
import { useRef } from "react";
import type { MutableRefObject, Dispatch, SetStateAction } from "react";
import { errorMessage } from "../../utils/errorMessage";

import {
  copyNodeDebugConfiguration,
  getNodeDebugConfiguration,
  importNodeDebugConfiguration,
} from "../../api";
import type { NodeDebugController } from "../../hooks/nodeDebug/useNodeDebugController";
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
      onStatusChange(t("debug.config.nameRequired"));
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
      onStatusChange(t("debug.config.noActive"));
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
    const message = t("debug.config.operationFailed", operation, errorMessage(cause));
    if (transferOwnerKeyRef.current !== mutationOwnerKey) return;
    setLocalNotice(message);
    onStatusChange(message);
    try {
      await refresh();
    } catch (refreshCause: unknown) {
      const refreshMessage = t("debug.sync.refreshFailed", message, errorMessage(refreshCause));
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
        onStatusChange(t("debug.config.exported", configuration.name));
      }
    } catch (cause: unknown) {
      const message = t("debug.config.exportFailed", errorMessage(cause));
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
      onStatusChange(t("debug.config.imported", configuration.name));
      try {
        await refresh();
      } catch (refreshCause: unknown) {
        const refreshMessage = t("debug.config.importedRefreshFailed", configuration.name, errorMessage(refreshCause));
        if (transferOwnerKeyRef.current === mutationOwnerKey) {
          setLocalNotice(refreshMessage);
          onStatusChange(refreshMessage);
        }
      }
      if (transferOwnerKeyRef.current === mutationOwnerKey && !nextState.active_configuration_id) {
        onSetConfigurationView();
      }
    } catch (cause: unknown) {
      await refreshAfterTransferFailure(t("debug.config.importOperation"), cause, mutationOwnerKey);
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
          activate: false,
        },
        workspaceId,
      );
      if (transferOwnerKeyRef.current === mutationOwnerKey) {
        onStatusChange(t("debug.config.copied", copied.name));
      }
    } catch (cause: unknown) {
      await refreshAfterTransferFailure(t("debug.config.copyOperation"), cause, mutationOwnerKey);
    }
  };

  return (
    <div className="node-debug-view node-debug-configuration-view" role="tabpanel">
      <section className="node-debug-section node-debug-scheme-section">
        <div className="debug-section-title-row">
          <h3>{t("debug.config.title")}</h3>
          <span>{t("debug.config.count", formatNumber(state?.configurations?.length ?? 0))}</span>
        </div>
        <div className="node-debug-scheme-list">
          {(state?.configurations ?? []).map((configuration) => (
            <div className={configuration.configuration_id === state?.active_configuration_id ? "active" : ""} key={configuration.configuration_id}>
              <button
                type="button"
                onClick={() => void activateConfiguration(configuration.configuration_id)}
                disabled={actionBusy || configurationLocked || configuration.configuration_id === state?.active_configuration_id}
                title={configurationLocked ? t("debug.config.locked") : t("debug.config.switch", configuration.name)}
              >
                <strong>{configuration.name}</strong>
                <small>{t("debug.config.breakpoints", configuration.script_path ?? t("debug.config.noEntry"), configuration.breakpoint_count === undefined ? "" : formatNumber(configuration.breakpoint_count))}</small>
              </button>
              <button
                type="button"
                className="danger"
                onClick={() => void deleteConfiguration(configuration.configuration_id)}
                disabled={actionBusy || (configurationLocked && configuration.configuration_id === state?.active_configuration_id)}
                aria-label={t("debug.config.delete", configuration.name)}
              >
                <span className="codicon codicon-trash" aria-hidden="true" />
              </button>
            </div>
          ))}
          {(state?.configurations?.length ?? 0) === 0 ? <span className="debug-muted">{t("debug.config.empty")}</span> : null}
        </div>
        <div className="node-debug-scheme-create">
          <input value={newConfigurationName} onChange={(event) => setNewConfigurationName(event.target.value)} placeholder={t("debug.config.newName")} />
          <button type="button" onClick={createScheme} disabled={!sessionId || actionBusy || configurationLocked}>{t("debug.config.create")}</button>
        </div>
        <p className="debug-muted">{t("debug.config.transferHint")}</p>
        {extensionWindow ? (
          <details className="node-debug-scheme-transfer">
            <summary>{t("debug.config.transfer")}</summary>
            <div>
              <button type="button" onClick={() => void exportActiveScheme()} disabled={!state?.active_configuration_id}>{t("debug.config.exportJson")}</button>
              <button type="button" onClick={() => importInputRef.current?.click()}>{t("debug.config.importJson")}</button>
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
                <option value="">{t("debug.config.copyTo")}</option>
                {sessions.filter((session) => session.session_id !== sessionId).map((session) => (
                  <option value={session.session_id} key={session.session_id}>{session.title}</option>
                ))}
              </select>
              <button type="button" onClick={() => void copyActiveScheme()} disabled={!state?.active_configuration_id || !copyTargetSessionId}>{t("debug.config.copy")}</button>
            </div>
          </details>
        ) : null}
      </section>
      <label>

        {t("debug.config.profile")}
        <select value={configurationName} onChange={(event) => handleProfileChange(event.target.value)}>
          {launchProfiles.map((profile) => (
            <option value={profile.name} key={profile.name}>{nodeDebugProfileLabel(profile)}</option>
          ))}
        </select>
      </label>
      <label>

        {t("debug.config.file")}
        <div className="node-debug-path-field">
          <input ref={scriptPathInputRef} value={scriptPath} onChange={(event) => { setLocalNotice(null); setScriptPath(event.target.value); }} placeholder={t("debug.config.pathHint")} />
          {activeFilePath ? <button type="button" onClick={() => { setLocalNotice(null); setScriptPath(activeFilePath); }}>{t("debug.config.currentFile")}</button> : null}
        </div>
      </label>
      <label>

        {t("debug.config.cwd")}
        <input value={workingDirectory} onChange={(event) => { setLocalNotice(null); setWorkingDirectory(event.target.value); }} placeholder={t("debug.config.cwdHint")} />
      </label>
      <label>

        {t("debug.config.arguments")}
        <input value={scriptArgs} onChange={(event) => { setLocalNotice(null); setScriptArgs(event.target.value); }} placeholder={t("debug.config.argumentsHint")} />
      </label>
      <button type="button" className="node-debug-start-button" onClick={onStartDebugging} disabled={!sessionId || loading || actionBusy || state?.status === "stopping" || state?.status === "reconcile_required" || capabilities?.enabled === false || activeProfile?.supported === false}>
        <span className="codicon codicon-debug-start" aria-hidden="true" />
        {processCanRestart ? t("debug.config.restart") : state?.status === "stopping" ? t("debug.status.stopping") : state?.status === "reconcile_required" ? t("debug.config.reconcileFirst") : t("debug.config.start")}
      </button>
      <button type="button" onClick={saveActiveScheme} disabled={!state?.active_configuration_id || actionBusy || configurationLocked}>

        {t("debug.config.save")}
      </button>
      {activeProfile?.supported === false ? <div className="debug-error">{t("debug.config.unsupported", activeProfile.adapter)}</div> : null}
      {capabilities?.enabled === false ? <div className="debug-error">{t("debug.config.disabled")}</div> : null}
    </div>
  );
}
