import { useCallback, useEffect, useMemo, useState } from "react";
import { getGatewayDiagnostics } from "../../gatewayApi";
import type {
  GatewayDiagnosticLog,
  GatewayDiagnostics,
  GatewayWorkspace,
} from "../../types/backend";
import { groupGatewayWorkspaces } from "./gatewayWorkspacePresentation";

interface GatewayDiagnosticsPanelProps {
  apiPort: number;
  workspaces: GatewayWorkspace[];
}

function formatBytes(value: number): string {
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
  return `${(value / (1024 * 1024)).toFixed(1)} MB`;
}

function formatTime(value: string | null): string {
  if (!value) return "暂无时间";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
}

function logStatusLabel(log: GatewayDiagnosticLog): string {
  if (log.status === "available") return "可读";
  if (log.status === "empty") return "空日志";
  return "不可用";
}

function diagnosticsStatusLabel(status: GatewayDiagnostics["status"]): string {
  if (status === "ready") return "正常";
  if (status === "degraded") return "部分异常";
  return "离线";
}

export default function GatewayDiagnosticsPanel({
  apiPort,
  workspaces,
}: GatewayDiagnosticsPanelProps) {
  const groups = useMemo(() => groupGatewayWorkspaces(workspaces), [workspaces]);
  const [gatewayConnectionId, setGatewayConnectionId] = useState<string | null>(null);
  const [workspaceId, setWorkspaceId] = useState("");
  const [selectedLogId, setSelectedLogId] = useState<string | null>(null);
  const [diagnostics, setDiagnostics] = useState<GatewayDiagnostics | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [copyNotice, setCopyNotice] = useState<string | null>(null);

  const selectedGroup = useMemo(
    () =>
      groups.find(
        (group) =>
          (gatewayConnectionId === null && group.key === "local") ||
          group.key === `remote:${gatewayConnectionId}`,
      ) ?? groups[0],
    [gatewayConnectionId, groups],
  );

  const loadDiagnostics = useCallback(
    async (logId?: string | null) => {
      setLoading(true);
      setError(null);
      try {
        const next = await getGatewayDiagnostics(apiPort, {
          gatewayConnectionId,
          workspaceId: workspaceId || null,
          logId: logId ?? null,
          tailLines: 300,
        });
        setDiagnostics(next);
        setSelectedLogId(next.selected_log_id);
      } catch (loadError) {
        setDiagnostics(null);
        setSelectedLogId(null);
        setError(loadError instanceof Error ? loadError.message : String(loadError));
      } finally {
        setLoading(false);
      }
    }, [apiPort, gatewayConnectionId, workspaceId],
  );

  useEffect(() => {
    void loadDiagnostics();
  }, [loadDiagnostics]);

  useEffect(() => {
    if (!selectedGroup) return;
    const stillExists = selectedGroup.workspaces.some(
      (workspace) => workspace.workspace_id === workspaceId,
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

  const diagnosticWorkspaceFor = (workspace: GatewayWorkspace) => {
    const diagnosticWorkspaceId = workspace.remote?.remote_workspace_id ?? workspace.workspace_id;
    return diagnostics?.workspaces.find((item) => item.workspace_id === diagnosticWorkspaceId);
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
    try {
      await navigator.clipboard.writeText(selectedLog.tail);
      setCopyNotice("当前日志尾部已复制。 ");
    } catch (copyError) {
      setCopyNotice(copyError instanceof Error ? copyError.message : String(copyError));
    }
  };

  return (
    <div className="gateway-diagnostics-panel" data-testid="gateway-diagnostics-panel">
      <section className="gateway-diagnostics-toolbar" aria-label="诊断范围">
        <label>
          <span>Gateway 连接</span>
          <select
            value={gatewayConnectionId ?? ""}
            onChange={(event) => selectGateway(event.target.value)}
          >
            {groups.map((group) => (
              <option key={group.key} value={group.key === "local" ? "" : group.key.slice("remote:".length)}>
                {group.title} · {group.workspaces.length} 个工作区
              </option>
            ))}
          </select>
        </label>
        <label>
          <span>工作区</span>
          <select value={workspaceId} onChange={(event) => selectWorkspace(event.target.value)}>
            <option value="">Gateway 日志 / 全部工作区</option>
            {selectedGroup?.workspaces.map((workspace) => (
              <option key={workspace.workspace_id} value={workspace.workspace_id}>
                {workspace.name}{workspace.managed ? " · 托管" : " · 外部"}
              </option>
            ))}
          </select>
        </label>
        <div className="gateway-diagnostics-toolbar-actions">
          <span className="gateway-diagnostics-readonly"><span className="codicon codicon-lock" aria-hidden="true" />只读查看</span>
          <button type="button" className="gateway-compact-button" disabled={loading} onClick={() => void loadDiagnostics(selectedLogId)}>
            <span className={`codicon codicon-refresh${loading ? " codicon-modifier-spin" : ""}`} aria-hidden="true" />
            {loading ? "读取中…" : "刷新日志"}
          </button>
        </div>
      </section>

      {error ? (
        <div className="gateway-console-alert" role="alert">
          <span className="codicon codicon-error" aria-hidden="true" />
          <div><strong>日志读取失败</strong><span>{error}</span></div>
        </div>
      ) : null}
      {copyNotice ? <div className="gateway-console-notice" role="status"><span className="codicon codicon-pass-filled" aria-hidden="true" />{copyNotice}</div> : null}

      {diagnostics ? (
        <>
          <section className="gateway-diagnostics-summary" aria-label="诊断摘要">
            <article>
              <span className="codicon codicon-server-process" aria-hidden="true" />
              <div><small>Gateway 状态</small><strong>{diagnosticsStatusLabel(diagnostics.status)}</strong><span>{diagnostics.gateway_name}</span></div>
            </article>
            <article>
              <span className="codicon codicon-briefcase" aria-hidden="true" />
              <div><small>工作区</small><strong>{diagnostics.workspaces.length}</strong><span>{offlineWorkspaceCount ? `${offlineWorkspaceCount} 个离线` : "全部可达"}</span></div>
            </article>
            <article>
              <span className="codicon codicon-output" aria-hidden="true" />
              <div><small>可读日志</small><strong>{availableLogCount}</strong><span>共 {diagnostics.logs.length} 个日志入口</span></div>
            </article>
            <article>
              <span className="codicon codicon-clock" aria-hidden="true" />
              <div><small>最近检查</small><strong>{formatTime(diagnostics.checked_at)}</strong><span>不会修改运行时状态</span></div>
            </article>
          </section>

          <section className="gateway-diagnostics-workspace-strip" aria-label="工作区状态">
            <div><strong>工作区连接状态</strong><span>Gateway 当前直接管理或投影的工作区</span></div>
            <div className="gateway-diagnostics-workspace-pills">
              {selectedGroup?.workspaces.map((workspace) => (
                <button
                  type="button"
                  key={workspace.workspace_id}
                  className={workspace.workspace_id === workspaceId ? "active" : undefined}
                  onClick={() => selectWorkspace(workspace.workspace_id)}
                >
                  <span className={`gateway-diagnostics-status-dot ${diagnosticWorkspaceFor(workspace)?.status ?? workspace.status}`} />
                  {workspace.name}
                </button>
              ))}
              {selectedGroup?.workspaces.length === 0 ? <span className="gateway-diagnostics-muted">当前 Gateway 没有可展示的工作区</span> : null}
            </div>
          </section>

          <section className="gateway-diagnostics-log-layout" aria-label="日志查看器">
            <aside className="gateway-diagnostics-log-list">
              <header><div><strong>日志入口</strong><span>{diagnostics.logs.length} 个</span></div><small>选择一项查看最新尾部</small></header>
              <div>
                {diagnostics.logs.map((log) => (
                  <button
                    type="button"
                    key={log.log_id}
                    className={log.log_id === selectedLog?.log_id ? "active" : undefined}
                    onClick={() => void loadDiagnostics(log.log_id)}
                  >
                    <span className={`codicon ${log.source === "gateway" ? "codicon-server-process" : "codicon-folder"}`} aria-hidden="true" />
                    <span className="gateway-diagnostics-log-copy"><strong>{log.label}</strong><small>{log.source === "gateway" ? "Gateway 控制面" : "工作区运行时"}</small></span>
                    <span className={`gateway-diagnostics-log-status ${log.status}`}>{logStatusLabel(log)}</span>
                  </button>
                ))}
              </div>
            </aside>
            <article className="gateway-diagnostics-viewer">
              {selectedLog ? (
                <>
                  <header>
                    <div><span>{selectedLog.source === "gateway" ? "Gateway 控制面" : selectedLog.workspace_name ?? "工作区"}</span><h2>{selectedLog.label}</h2><small>{selectedLog.size_bytes ? `${formatBytes(selectedLog.size_bytes)} · ` : ""}{formatTime(selectedLog.updated_at)}{selectedLog.truncated ? " · 仅显示最新尾部" : ""}</small></div>
                    <button type="button" className="gateway-compact-button" disabled={!selectedLog.tail} onClick={() => void copySelectedLog()}><span className="codicon codicon-copy" aria-hidden="true" />复制</button>
                  </header>
                  {selectedLog.status === "unavailable" ? (
                    <div className="gateway-diagnostics-viewer-empty"><span className="codicon codicon-warning" aria-hidden="true" /><strong>当前无法读取这份日志</strong><p>{selectedLog.error ?? "Gateway 没有返回日志内容。"}</p></div>
                  ) : selectedLog.status === "empty" ? (
                    <div className="gateway-diagnostics-viewer-empty"><span className="codicon codicon-output" aria-hidden="true" /><strong>日志文件为空</strong><p>服务可能尚未输出内容，刷新后会重新读取。</p></div>
                  ) : (
                    <pre>{selectedLog.tail || "暂无日志内容"}</pre>
                  )}
                </>
              ) : (
                <div className="gateway-diagnostics-viewer-empty"><span className="codicon codicon-output" aria-hidden="true" /><strong>暂无日志入口</strong><p>当前 Gateway 没有返回可查看的日志文件。</p></div>
              )}
            </article>
          </section>
        </>
      ) : (
        <div className="gateway-diagnostics-loading"><span className="codicon codicon-loading codicon-modifier-spin" aria-hidden="true" />正在读取 Gateway 诊断信息…</div>
      )}
    </div>
  );
}
