import { gatewayWorkspaceKey } from "../../../api/workspaceIdentity";
import { t } from "../../../../../shared/core/i18n/index.js";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { getGatewayDiagnostics } from "../../../gatewayApi";
import type {
  GatewayDiagnostics,
  GatewayWorkspace,
} from "../../../types/backend";
import { groupGatewayWorkspaces } from "./gatewayWorkspacePresentation";
import { formatByteSize, formatLocalTimestamp } from "../../../utils/format";
import {
  diagnosticLogStatusLabel,
  diagnosticLogUnavailableHint,
} from "../../panels/gatewayLogPresentation";
import { errorMessage } from "../../../utils/errorMessage";
import { copyTextToClipboard } from "../../../utils/clipboard";

interface GatewayDiagnosticsPanelProps {
  apiPort: number;
  workspaces: GatewayWorkspace[];
  initialConnectionId?: string | null;
  initialWorkspaceId?: string;
}

function formatTime(value: string | null): string {
  if (!value) return t("gateway.diagnostics.noTime");
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : formatLocalTimestamp(value);
}

function diagnosticsStatusLabel(status: GatewayDiagnostics["status"]): string {
  if (status === "ready") return t("gateway.diagnostics.statusReady");
  if (status === "degraded") return t("gateway.diagnostics.statusDegraded");
  return t("gateway.diagnostics.statusOffline");
}

export default function GatewayDiagnosticsPanel({
  apiPort,
  workspaces,
  initialConnectionId = null,
  initialWorkspaceId = "",
}: GatewayDiagnosticsPanelProps) {
  const groups = useMemo(() => groupGatewayWorkspaces(workspaces), [workspaces]);
  const [gatewayConnectionId, setGatewayConnectionId] = useState<string | null>(initialConnectionId);
  const [workspaceId, setWorkspaceId] = useState(initialWorkspaceId);
  const [selectedLogId, setSelectedLogId] = useState<string | null>(null);
  const [diagnostics, setDiagnostics] = useState<GatewayDiagnostics | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [copyNotice, setCopyNotice] = useState<string | null>(null);
  // 诊断范围可被连续切换；只有最后一次发起的响应才允许写入展示态，否则先发后到的
  // 旧范围响应会覆盖新范围，用户看到与当前选择不符的工作区/日志。
  const requestRevisionRef = useRef(0);
  // 上一次渲染的诊断范围。范围一变化必须在同一提交内作废旧快照：仅靠 revision
  // 只能丢弃先发后到的旧响应，但「新范围选择 + 旧 diagnostics」仍会让 selectedLog
  // 回退到 diagnostics.logs[0]，在新响应到达前继续渲染旧工作区/旧日志入口的 tail。
  const lastScopeRef = useRef(`${gatewayConnectionId ?? ""}:${workspaceId}`);
  const scopeKey = `${gatewayConnectionId ?? ""}:${workspaceId}`;
  if (lastScopeRef.current !== scopeKey) {
    lastScopeRef.current = scopeKey;
    requestRevisionRef.current += 1;
    setDiagnostics(null);
    setSelectedLogId(null);
    setError(null);
    setLoading(true);
  }

  const selectedGroup = useMemo(
    () =>
      groups.find(
        (group) =>
          (gatewayConnectionId === null && group.key === "local") ||
          group.key === `remote:${gatewayConnectionId}`,
      ),
    [gatewayConnectionId, groups],
  );

  const loadDiagnostics = useCallback(
    async (logId?: string | null) => {
      const revision = requestRevisionRef.current + 1;
      requestRevisionRef.current = revision;
      setLoading(true);
      setError(null);
      try {
        const next = await getGatewayDiagnostics(apiPort, {
          gatewayConnectionId,
          workspaceId: workspaceId || null,
          logId: logId ?? null,
          tailLines: 300,
        });
        if (requestRevisionRef.current !== revision) return;
        setDiagnostics(next);
        setSelectedLogId(next.selected_log_id);
      } catch (loadError) {
        if (requestRevisionRef.current !== revision) return;
        setError(errorMessage(loadError));
      } finally {
        if (requestRevisionRef.current === revision) setLoading(false);
      }
    }, [apiPort, gatewayConnectionId, workspaceId],
  );

  useEffect(() => {
    void loadDiagnostics();
  }, [loadDiagnostics]);

  useEffect(() => {
    if (!selectedGroup) return;
    const stillExists = selectedGroup.workspaces.some(
      (workspace) => gatewayWorkspaceKey(workspace) === workspaceId,
    );
    if (workspaceId && !stillExists) setWorkspaceId("");
  }, [selectedGroup, workspaceId]);

  const selectedLog = useMemo(() => {
    if (!diagnostics) return null;
    const id = selectedLogId ?? diagnostics.selected_log_id;
    return diagnostics.logs.find((log) => log.log_id === id) ?? diagnostics.logs[0] ?? null;
  }, [diagnostics, selectedLogId]);

  const availableLogCount = diagnostics?.logs.filter((log) => log.status === "available").length ?? 0;
  const offlineWorkspaceCount = diagnostics?.workspaces.filter((item) => item.status === "offline").length ?? 0;
  const diagnosticWorkspacesById = useMemo(() => {
    const workspacesById = new Map<string, GatewayDiagnostics["workspaces"][number]>();
    for (const item of diagnostics?.workspaces ?? []) {
      if (!workspacesById.has(item.workspace_id)) {
        workspacesById.set(item.workspace_id, item);
      }
    }
    return workspacesById;
  }, [diagnostics?.workspaces]);

  const diagnosticWorkspaceFor = (workspace: GatewayWorkspace) => {
    const diagnosticWorkspaceId = workspace.workspace_id;
    return diagnosticWorkspacesById.get(diagnosticWorkspaceId);
  };

  const selectGateway = (value: string) => {
    setGatewayConnectionId(value || null);
    setWorkspaceId("");
    setSelectedLogId(null);
    setCopyNotice(null);
  };

  const selectWorkspace = (value: string) => {
    setWorkspaceId(value);
    setSelectedLogId(null);
    setCopyNotice(null);
  };

  const copySelectedLog = async () => {
    if (!selectedLog?.tail) return;
    // 复制统一走 utils/clipboard 的唯一实现：非安全上下文下 navigator.clipboard 真实
    // 缺失，必须回退兼容复制，不能把「没有 Clipboard API」当 TypeError 暴露给用户。
    try {
      await copyTextToClipboard(selectedLog.tail);
      setCopyNotice(t("gateway.diagnostics.logCopied"));
    } catch (copyError) {
      setCopyNotice(errorMessage(copyError));
    }
  };

  return (
    <div className="gateway-diagnostics-panel" data-testid="gateway-diagnostics-panel">
      <section className="gateway-diagnostics-toolbar" aria-label={t("gateway.diagnostics.scope")}>
        <label>
          <span>{t("gateway.diagnostics.gatewayConnection")}</span>
          <select
            value={gatewayConnectionId ?? ""}
            onChange={(event) => selectGateway(event.target.value)}
          >
            {groups.map((group) => (
              <option key={group.key} value={group.key === "local" ? "" : group.key.slice("remote:".length)}>
                {group.title} · {t("gateway.diagnostics.workspaceCount", group.workspaces.length)}
              </option>
            ))}
            {gatewayConnectionId && !selectedGroup ? <option value={gatewayConnectionId}>{gatewayConnectionId}</option> : null}
          </select>
        </label>
        <label>
          <span>{t("gateway.diagnostics.workspace")}</span>
          <select value={workspaceId} onChange={(event) => selectWorkspace(event.target.value)}>
            <option value="">{t("gateway.diagnostics.allWorkspaces")}</option>
            {selectedGroup?.workspaces.map((workspace) => (
              <option key={gatewayWorkspaceKey(workspace)} value={gatewayWorkspaceKey(workspace)}>
                {workspace.name}{workspace.managed ? t("gateway.diagnostics.managed") : t("gateway.diagnostics.external")}
              </option>
            ))}
          </select>
        </label>
        <div className="gateway-diagnostics-toolbar-actions">
          <span className="gateway-diagnostics-readonly"><span className="codicon codicon-lock" aria-hidden="true" />{t("gateway.diagnostics.readOnly")}</span>
          <button type="button" className="gateway-compact-button" disabled={loading} onClick={() => void loadDiagnostics(selectedLogId)}>
            <span className={`codicon codicon-refresh${loading ? " codicon-modifier-spin" : ""}`} aria-hidden="true" />
            {loading ? t("gateway.diagnostics.loading") : t("gateway.diagnostics.refreshLogs")}
          </button>
        </div>
      </section>

      <div className="gateway-console-feedback-slot">
        {error ? (
          <div
            className="gateway-console-feedback error"
            role="alert"
            title={t(diagnostics ? "gateway.diagnostics.refreshFailedWithSnapshot" : "gateway.diagnostics.readFailed", error)}
          >
            <span className="codicon codicon-error" aria-hidden="true" />
            <strong>{diagnostics ? t("gateway.diagnostics.refreshFailed") : t("gateway.diagnostics.readFailedTitle")}</strong>
            <span className="gateway-console-feedback-message">{error}</span>
          </div>
        ) : null}
        {copyNotice ? (
          <div className="gateway-console-feedback notice" role="status" title={copyNotice}>
            <span className="codicon codicon-pass-filled" aria-hidden="true" />
            <span className="gateway-console-feedback-message">{copyNotice}</span>
          </div>
        ) : null}
      </div>

      {diagnostics ? (
        <>
          <section className="gateway-diagnostics-summary" aria-label={t("gateway.diagnostics.summary")}>
            <article>
              <span className="codicon codicon-server-process" aria-hidden="true" />
              <div><small>{t("gateway.diagnostics.gatewayStatus")}</small><strong>{diagnosticsStatusLabel(diagnostics.status)}</strong><span>{diagnostics.gateway_name}</span></div>
            </article>
            <article>
              <span className="codicon codicon-briefcase" aria-hidden="true" />
              <div><small>{t("gateway.diagnostics.workspace")}</small><strong>{diagnostics.workspaces.length}</strong><span>{offlineWorkspaceCount ? t("gateway.diagnostics.offlineCount", offlineWorkspaceCount) : t("gateway.diagnostics.allReachable")}</span></div>
            </article>
            <article>
              <span className="codicon codicon-output" aria-hidden="true" />
              <div><small>{t("gateway.diagnostics.readableLogs")}</small><strong>{availableLogCount}</strong><span>{t("gateway.diagnostics.logEntryCount", diagnostics.logs.length)}</span></div>
            </article>
            <article>
              <span className="codicon codicon-clock" aria-hidden="true" />
              <div><small>{t("gateway.diagnostics.lastCheck")}</small><strong>{formatTime(diagnostics.checked_at)}</strong><span>{t("gateway.diagnostics.readOnlyRuntime")}</span></div>
            </article>
          </section>

          <section className="gateway-diagnostics-workspace-strip" aria-label={t("gateway.diagnostics.workspaceStatus")}>
            <div><strong>{t("gateway.diagnostics.workspaceConnectionStatus")}</strong><span>{t("gateway.diagnostics.workspaceProjectionDescription")}</span></div>
            <div className="gateway-diagnostics-workspace-pills">
              {selectedGroup?.workspaces.map((workspace) => (
                <button
                  type="button"
                  key={gatewayWorkspaceKey(workspace)}
                  className={gatewayWorkspaceKey(workspace) === workspaceId ? "active" : undefined}
                  onClick={() => selectWorkspace(gatewayWorkspaceKey(workspace))}
                >
                  <span className={`gateway-diagnostics-status-dot ${diagnosticWorkspaceFor(workspace)?.status ?? workspace.status}`} />
                  {workspace.name}
                </button>
              ))}
              {selectedGroup?.workspaces.length === 0 ? <span className="gateway-diagnostics-muted">{t("gateway.diagnostics.noWorkspaces")}</span> : null}
            </div>
          </section>

          <details className="gateway-diagnostics-runtime" open>
            <summary>{t("gateway.diagnostics.processLogSettings")}</summary>
            {diagnostics.processes.map((process) => (
              <div key={`${process.service}:${process.state?.runtime_instance_id ?? process.workspace_ids.join(",")}`}>
                <strong>{process.service}</strong>
                {process.state ? (
                  <>
                    <p>{t("gateway.diagnostics.processInstance", process.state.pid, process.state.runtime_instance_id)}</p>
                    <p>{t("gateway.diagnostics.processRevisionDetails", process.state.applied_revision ?? "bootstrap", process.state.level.toUpperCase(), process.state.pretty ? t("gateway.diagnostics.prettyFormat") : t("gateway.diagnostics.jsonFormat"))}</p>
                    {process.state.log_sink ? (
                      <><p>{t("gateway.diagnostics.logEntry")}{process.state.log_sink.log_id ?? t("gateway.diagnostics.noLogForScope")}</p><p>{t("gateway.diagnostics.processLogOwner", process.state.log_sink.owner_id)} · {t("gateway.diagnostics.source")}{process.state.log_sink.source}</p><p>{t("gateway.diagnostics.fileIdentity")}{process.state.log_sink.device}:{process.state.log_sink.inode}</p></>
                    ) : <p>{t("gateway.diagnostics.stdoutOnly", process.output_mode)}</p>}
                  </>
                ) : <p role="alert">{process.error}</p>}
                {process.workspace_ids.length ? <p>{t("gateway.diagnostics.mountedWorkspaces")}{process.workspace_ids.join(", ")}</p> : null}
              </div>
            ))}
          </details>

          <section className="gateway-diagnostics-log-layout" aria-label={t("gateway.diagnostics.logViewer")}>
            <aside className="gateway-diagnostics-log-list">
              <header><div><strong>{t("gateway.diagnostics.logEntries")}</strong><span>{t("gateway.diagnostics.logEntryCount", diagnostics.logs.length)}</span></div><small>{t("gateway.diagnostics.selectLogHint")}</small></header>
              <div>
                {diagnostics.logs.map((log) => (
                  <button
                    type="button"
                    key={log.log_id}
                    className={log.log_id === selectedLog?.log_id ? "active" : undefined}
                    onClick={() => void loadDiagnostics(log.log_id)}
                  >
                    <span className={`codicon ${log.source === "gateway" ? "codicon-server-process" : "codicon-folder"}`} aria-hidden="true" />
                    <span className="gateway-diagnostics-log-copy"><strong>{log.label}</strong><small>{log.source === "gateway" ? t("gateway.diagnostics.gatewayControlPlane") : t("gateway.diagnostics.workspaceRuntime")}</small></span>
                    <span className={`gateway-diagnostics-log-status ${log.status}`}>{diagnosticLogStatusLabel(log)}</span>
                  </button>
                ))}
              </div>
            </aside>
            <article className="gateway-diagnostics-viewer">
              {selectedLog ? (
                <>
                  <header>
                    <div><span>{selectedLog.source === "gateway" ? t("gateway.diagnostics.gatewayControlPlane") : selectedLog.workspace_name ?? t("gateway.diagnostics.workspace")}</span><h2>{selectedLog.label}</h2><small>{selectedLog.size_bytes ? `${formatByteSize(selectedLog.size_bytes)} · ` : ""}{formatTime(selectedLog.updated_at)}{selectedLog.truncated ? ` · ${t("gateway.diagnostics.latestTailOnly")}` : ""}</small></div>
                    <button type="button" className="gateway-compact-button" disabled={!selectedLog.tail} onClick={() => void copySelectedLog()}><span className="codicon codicon-copy" aria-hidden="true" />{t("gateway.diagnostics.copy")}</button>
                  </header>
                  {selectedLog.status === "unavailable" ? (
                    <div className="gateway-diagnostics-viewer-empty"><span className="codicon codicon-warning" aria-hidden="true" /><strong>{t("gateway.diagnostics.logUnavailable")}</strong><p>{diagnosticLogUnavailableHint(selectedLog)}</p></div>
                  ) : selectedLog.status === "empty" ? (
                    <div className="gateway-diagnostics-viewer-empty"><span className="codicon codicon-output" aria-hidden="true" /><strong>{t("gateway.diagnostics.emptyLog")}</strong><p>{t("gateway.diagnostics.emptyLogDetail")}</p></div>
                  ) : (
                    <pre>{selectedLog.tail || t("gateway.diagnostics.noLogContent")}</pre>
                  )}
                </>
              ) : (
                <div className="gateway-diagnostics-viewer-empty"><span className="codicon codicon-output" aria-hidden="true" /><strong>{t("gateway.diagnostics.noLogEntries")}</strong><p>{t("gateway.diagnostics.noLogEntriesDetail")}</p></div>
              )}
            </article>
          </section>
        </>
      ) : (
        error ? (
          <div className="gateway-diagnostics-viewer-empty" role="status">
            <span className="codicon codicon-warning" aria-hidden="true" />
            <strong>{t("gateway.diagnostics.noSnapshot")}</strong>
            <p>{t("gateway.diagnostics.refreshToRetry")}</p>
          </div>
        ) : (
          <div className="gateway-diagnostics-loading" role="status">
            <span className="codicon codicon-loading codicon-modifier-spin" aria-hidden="true" />
            {t("gateway.diagnostics.loadingDiagnostics")}
          </div>
        )
      )}
    </div>
  );
}
