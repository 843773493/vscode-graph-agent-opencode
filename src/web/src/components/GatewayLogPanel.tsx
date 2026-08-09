import { useCallback, useEffect, useMemo, useState } from "react";
import { getGatewayDiagnostics } from "../gatewayApi";
import type {
  GatewayDiagnosticLog,
  GatewayDiagnostics,
  GatewayWorkspace,
} from "../types/backend";
import { groupGatewayWorkspaces } from "./workspace/gatewayWorkspacePresentation";

interface GatewayLogPanelProps {
  apiPort: number;
  workspaces: GatewayWorkspace[];
  height: number;
  onClose: () => void;
}

function formatTime(value: string | null): string {
  if (!value) return "暂无时间";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
}

function statusLabel(status: GatewayDiagnostics["status"]): string {
  if (status === "ready") return "正常";
  if (status === "degraded") return "部分异常";
  return "离线";
}

function logStatusLabel(log: GatewayDiagnosticLog): string {
  if (log.status === "available") return "可读";
  if (log.status === "empty") return "空日志";
  return "不可用";
}

type GatewayAttentionKind = "error" | "warning";

interface GatewayAttentionLine {
  kind: GatewayAttentionKind;
  rawText: string;
  text: string;
  occurrences: number;
}

function getAttentionLines(log: GatewayDiagnosticLog | null): GatewayAttentionLine[] {
  if (!log?.tail) return [];
  const lines = new Map<string, GatewayAttentionLine>();
  for (const rawLine of log.tail.split("\n")) {
    const rawText = rawLine.replace(/\s+/g, " ").trim();
    if (!rawText) continue;
    const error = /\b(?:ERROR|CRITICAL|Traceback|Exception)\b|status=5\d{2}\b|HTTP\/\d(?:\.\d)?\s+[\"']?5\d{2}\b/i.test(rawText);
    const warning = /\b(?:WARN(?:ING)?|failed|failure|timeout)\b|status=4\d{2}\b|HTTP\/\d(?:\.\d)?\s+[\"']?4\d{2}\b/i.test(rawText);
    if (!error && !warning) continue;
    const text = rawText
      .replace(/^\d{4}-\d{2}-\d{2}T\S+\s+/, "")
      .replace(/\b(?:WARNING|WARN|ERROR|CRITICAL)\b\s*/i, "")
      .replace(/\s+workspace_id=\S+/g, "")
      .replace(/,\s*error=ConnectError: All connection attempts failed/gi, "：连接失败")
      .replace(/\s+request_id=\S+/g, " request_id=…")
      .trim();
    const existing = lines.get(text);
    if (existing) {
      existing.occurrences += 1;
      continue;
    }
    if (lines.size >= 6) continue;
    lines.set(text, { kind: error ? "error" : "warning", rawText, text, occurrences: 1 });
  }
  return [...lines.values()];
}

function getGatewayAttentionCount(
  diagnostics: GatewayDiagnostics | null,
  attentionLines: GatewayAttentionLine[],
): number {
  const offlineWorkspaces = diagnostics?.workspaces.filter(
    (workspace) => workspace.status === "offline" || workspace.connection_error,
  ).length ?? 0;
  const unavailableLogs = diagnostics?.logs.filter((log) => log.status === "unavailable").length ?? 0;
  return attentionLines.length + offlineWorkspaces + unavailableLogs;
}

interface GatewayLogSummaryProps {
  diagnostics: GatewayDiagnostics | null;
  selectedLog: GatewayDiagnosticLog | null;
  attentionLines: GatewayAttentionLine[];
  attentionCount: number;
  onOpenRawLogs: () => void;
}

function GatewayLogSummary({
  diagnostics,
  selectedLog,
  attentionLines,
  attentionCount,
  onOpenRawLogs,
}: GatewayLogSummaryProps) {
  const offlineWorkspaces = diagnostics?.workspaces.filter(
    (workspace) => workspace.status === "offline" || workspace.connection_error,
  ) ?? [];
  const unavailableLogs = diagnostics?.logs.filter((log) => log.status === "unavailable") ?? [];
  const visibleOfflineWorkspaces = offlineWorkspaces.slice(0, 3);
  const visibleUnavailableLogs = unavailableLogs.slice(0, 3);
  const visibleFallbackCount = visibleOfflineWorkspaces.length + visibleUnavailableLogs.length;
  const visibleAttentionCount = attentionLines.length > 0
    ? attentionLines.length
    : visibleFallbackCount;
  const hiddenAttentionCount = Math.max(0, attentionCount - visibleAttentionCount);
  return (
    <div className="gateway-log-panel-summary" aria-label="Gateway 重点摘要">
      <div className={`gateway-log-panel-summary-hero ${diagnostics?.status ?? "offline"}`}>
        <div>
          <span className="gateway-log-panel-summary-kicker">当前状态</span>
          <strong>{diagnostics ? statusLabel(diagnostics.status) : "正在检查"}</strong>
          <span>{diagnostics?.gateway_name ?? "Gateway"}</span>
        </div>
        <button type="button" className="gateway-log-panel-summary-raw-button" onClick={onOpenRawLogs}>
          查看原始日志
        </button>
      </div>

      <div className="gateway-log-panel-summary-metrics">
        <div>
          <span>工作区</span>
          <strong>{diagnostics?.workspaces.length ?? "—"}</strong>
        </div>
        <div>
          <span>日志入口</span>
          <strong>{diagnostics?.logs.length ?? "—"}</strong>
        </div>
        <div className={attentionCount > 0 ? "attention" : "ok"}>
          <span>需关注</span>
          <strong>{diagnostics ? attentionCount : "—"}</strong>
        </div>
      </div>

      <section className="gateway-log-panel-summary-section">
        <header>
          <strong>重点</strong>
          <span>{selectedLog ? `检查 ${selectedLog.label}` : "等待检查结果"}</span>
        </header>
        {attentionLines.length > 0 ? (
          <div className="gateway-log-panel-summary-items">
            {attentionLines.map((line) => (
              <div className={`gateway-log-panel-summary-item ${line.kind}`} key={`${line.kind}:${line.text}`}>
                <span className="gateway-log-panel-summary-item-dot" />
                <span>{line.text}{line.occurrences > 1 ? ` ×${line.occurrences}` : ""}</span>
              </div>
            ))}
          </div>
        ) : offlineWorkspaces.length > 0 || unavailableLogs.length > 0 ? (
          <div className="gateway-log-panel-summary-items">
            {visibleOfflineWorkspaces.map((workspace) => (
              <div className="gateway-log-panel-summary-item error" key={`workspace:${workspace.workspace_id}`}>
                <span className="gateway-log-panel-summary-item-dot" />
                <span>{workspace.name}：工作区连接异常{workspace.connection_error ? ` · ${workspace.connection_error}` : ""}</span>
              </div>
            ))}
            {visibleUnavailableLogs.map((log) => (
              <div className="gateway-log-panel-summary-item warning" key={`log:${log.log_id}`}>
                <span className="gateway-log-panel-summary-item-dot" />
                <span>{log.label}：日志不可读</span>
              </div>
            ))}
          </div>
        ) : (
          <div className="gateway-log-panel-summary-empty">
            <span className="codicon codicon-check" aria-hidden="true" />
            <span>当前没有检测到错误或警告，原始请求日志已收起。</span>
          </div>
        )}
        {hiddenAttentionCount > 0 ? (
          <div className="gateway-log-panel-summary-overflow">
            还有 {hiddenAttentionCount} 项需关注；打开原始日志查看完整入口。
          </div>
        ) : null}
      </section>

      <p className="gateway-log-panel-summary-hint">
        只在需要排查请求细节时打开原始日志；列表中的日志入口不会默认全部展开。
      </p>
    </div>
  );
}

export default function GatewayLogPanel({
  apiPort,
  workspaces,
  height,
  onClose,
}: GatewayLogPanelProps) {
  const groups = useMemo(() => groupGatewayWorkspaces(workspaces), [workspaces]);
  const [gatewayConnectionId, setGatewayConnectionId] = useState<string | null>(null);
  const [workspaceId, setWorkspaceId] = useState("");
  const [selectedLogId, setSelectedLogId] = useState<string | null>(null);
  const [diagnostics, setDiagnostics] = useState<GatewayDiagnostics | null>(null);
  const [loading, setLoading] = useState(false);
  const [autoRefresh, setAutoRefresh] = useState(true);
  const [viewMode, setViewMode] = useState<"summary" | "raw">("summary");
  const [rawFilter, setRawFilter] = useState<"all" | "attention">("all");
  const [summaryAttentionCount, setSummaryAttentionCount] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const [copyNotice, setCopyNotice] = useState<string | null>(null);

  const selectedGroup = useMemo(
    () => groups.find(
      (group) => gatewayConnectionId === null
        ? group.key === "local"
        : group.key === `remote:${gatewayConnectionId}`,
    ) ?? groups[0],
    [gatewayConnectionId, groups],
  );

  const loadDiagnostics = useCallback(
    async (logId: string | null, silent = false) => {
      if (!silent) setLoading(true);
      setError(null);
      try {
        const next = await getGatewayDiagnostics(apiPort, {
          gatewayConnectionId,
          workspaceId: workspaceId || null,
          logId,
          tailLines: 300,
        });
        setDiagnostics(next);
        setSelectedLogId(next.selected_log_id);
      } catch (loadError) {
        setError(loadError instanceof Error ? loadError.message : String(loadError));
        if (!silent) {
          setDiagnostics(null);
          setSelectedLogId(null);
        }
      } finally {
        if (!silent) setLoading(false);
      }
    },
    [apiPort, gatewayConnectionId, workspaceId],
  );

  useEffect(() => {
    void loadDiagnostics(null);
  }, [loadDiagnostics]);

  useEffect(() => {
    if (!autoRefresh) return;
    const intervalId = window.setInterval(() => {
      void loadDiagnostics(selectedLogId, true);
    }, 3000);
    return () => window.clearInterval(intervalId);
  }, [autoRefresh, loadDiagnostics, selectedLogId]);

  useEffect(() => {
    if (!selectedGroup || !workspaceId) return;
    if (!selectedGroup.workspaces.some((workspace) => workspace.workspace_id === workspaceId)) {
      setWorkspaceId("");
    }
  }, [selectedGroup, workspaceId]);

  const selectedLog = useMemo(() => {
    if (!diagnostics) return null;
    return diagnostics.logs.find((log) => log.log_id === selectedLogId)
      ?? diagnostics.logs[0]
      ?? null;
  }, [diagnostics, selectedLogId]);
  const attentionLines = useMemo(() => getAttentionLines(selectedLog), [selectedLog]);
  const attentionCount = useMemo(
    () => getGatewayAttentionCount(diagnostics, attentionLines),
    [attentionLines, diagnostics],
  );
  useEffect(() => {
    if (viewMode === "summary") {
      setSummaryAttentionCount(attentionCount);
    }
  }, [attentionCount, viewMode]);
  const overviewAttentionCount = viewMode === "raw" && summaryAttentionCount > 0
    ? summaryAttentionCount
    : attentionCount;

  const selectGateway = (value: string) => {
    setGatewayConnectionId(value || null);
    setWorkspaceId("");
    setSelectedLogId(null);
    setCopyNotice(null);
    setRawFilter("all");
  };

  const selectWorkspace = (value: string) => {
    setWorkspaceId(value);
    setSelectedLogId(null);
    setCopyNotice(null);
    setRawFilter("all");
  };

  const copySelectedLog = async () => {
    if (!selectedLog?.tail) return;
    try {
      await navigator.clipboard.writeText(selectedLog.tail);
      setCopyNotice("已复制当前日志尾部");
    } catch (copyError) {
      setCopyNotice(`复制失败：${copyError instanceof Error ? copyError.message : String(copyError)}`);
    }
    window.setTimeout(() => setCopyNotice(null), 1800);
  };

  return (
    <section
      className="gateway-log-panel"
      style={{ flexBasis: `${height}px` }}
      data-testid="gateway-log-panel"
    >
      <header className="gateway-log-panel-header">
        <div className="gateway-log-panel-tabs" role="tablist" aria-label="底部面板">
          <button type="button" className="gateway-log-panel-tab active" role="tab" aria-selected="true">
            <span className="codicon codicon-output" aria-hidden="true" />
            <span>Gateway 状态</span>
            {diagnostics ? (
              <span className="gateway-log-panel-count">
                {viewMode === "summary"
                  ? `重点 ${attentionCount}`
                  : `重点 ${overviewAttentionCount} · ${diagnostics.logs.length} 项`}
              </span>
            ) : null}
          </button>
        </div>
        <div className="gateway-log-panel-actions">
          {diagnostics ? (
            <span className={`gateway-log-panel-status ${diagnostics.status}`}>
              <span className="gateway-log-panel-status-dot" />
              {diagnostics.gateway_name} · {statusLabel(diagnostics.status)}
            </span>
          ) : null}
          <button
            type="button"
            className={`gateway-log-panel-icon-button${autoRefresh ? " active" : ""}`}
            title={autoRefresh ? "关闭自动刷新" : "开启自动刷新"}
            aria-label={autoRefresh ? "关闭自动刷新" : "开启自动刷新"}
            aria-pressed={autoRefresh}
            onClick={() => setAutoRefresh((value) => !value)}
          >
            <span className="codicon codicon-sync" aria-hidden="true" />
          </button>
          <button
            type="button"
            className="gateway-log-panel-icon-button"
            title={loading ? "正在刷新 Gateway" : "刷新 Gateway 日志"}
            aria-label={loading ? "正在刷新 Gateway" : "刷新 Gateway 日志"}
            disabled={loading}
            onClick={() => void loadDiagnostics(selectedLogId)}
          >
            <span className={`codicon codicon-refresh${loading ? " codicon-modifier-spin" : ""}`} aria-hidden="true" />
          </button>
          <button
            type="button"
            className="gateway-log-panel-icon-button"
            title="切换底部面板"
            aria-label="切换底部面板"
            onClick={onClose}
          >
            <span className="codicon codicon-close" aria-hidden="true" />
          </button>
        </div>
      </header>

      <div className="gateway-log-panel-toolbar">
        <label>
          <span>Gateway</span>
          <select
            value={gatewayConnectionId ?? ""}
            onChange={(event) => selectGateway(event.target.value)}
          >
            {groups.map((group) => (
              <option key={group.key} value={group.key === "local" ? "" : group.key.slice("remote:".length)}>
                {group.title}
              </option>
            ))}
          </select>
        </label>
        <label>
          <span>工作区</span>
          <select value={workspaceId} onChange={(event) => selectWorkspace(event.target.value)}>
            <option value="">全部工作区</option>
            {selectedGroup?.workspaces.map((workspace) => (
              <option key={workspace.workspace_id} value={workspace.workspace_id}>
                {workspace.name}
              </option>
            ))}
          </select>
        </label>
        <span className="gateway-log-panel-toolbar-meta">
          {loading
            ? "正在刷新 Gateway…"
            : diagnostics
              ? `最近检查 ${formatTime(diagnostics.checked_at)}`
              : "正在连接 Gateway…"}
          {copyNotice ? ` · ${copyNotice}` : ""}
        </span>
        <div className="gateway-log-panel-mode-switch" role="group" aria-label="日志显示方式">
          <button
            type="button"
            className={viewMode === "summary" ? "active" : ""}
            aria-pressed={viewMode === "summary"}
            onClick={() => setViewMode("summary")}
          >
            重点
          </button>
          <button
            type="button"
            className={viewMode === "raw" ? "active" : ""}
            aria-pressed={viewMode === "raw"}
            onClick={() => setViewMode("raw")}
          >
            原始日志
          </button>
        </div>
      </div>

      {error ? (
        <div className="gateway-log-panel-alert" role="alert">
          <span className="codicon codicon-error" aria-hidden="true" />
          <span>Gateway 日志读取失败：{error}</span>
        </div>
      ) : null}

      {viewMode === "raw" ? (
        <div className="gateway-log-panel-raw-context" role="status" aria-live="polite">
          <span><strong>重点摘要保留 {overviewAttentionCount} 项</strong> · 当前日志重点 {attentionLines.length} 项</span>
          <button type="button" onClick={() => setViewMode("summary")}>返回重点</button>
        </div>
      ) : null}

      {viewMode === "summary" ? (
        <GatewayLogSummary
          diagnostics={diagnostics}
          selectedLog={selectedLog}
          attentionLines={attentionLines}
          attentionCount={attentionCount}
          onOpenRawLogs={() => setViewMode("raw")}
        />
      ) : (
      <div className="gateway-log-panel-content">
        <aside className="gateway-log-panel-list" aria-label="Gateway 日志入口">
          <div className="gateway-log-panel-list-heading">
            <strong>日志入口</strong>
            <span>{diagnostics?.logs.length ?? 0}</span>
          </div>
          <div className="gateway-log-panel-list-scroll">
            {diagnostics?.logs.map((log) => (
              <button
                type="button"
                key={log.log_id}
                className={log.log_id === selectedLog?.log_id ? "active" : ""}
                title={`打开日志：${log.label}`}
                aria-label={`打开日志：${log.label}`}
                onClick={() => void loadDiagnostics(log.log_id)}
              >
                <span className={`codicon ${log.source === "gateway" ? "codicon-server-process" : "codicon-folder"}`} aria-hidden="true" />
                <span className="gateway-log-panel-list-copy">
                  <strong>{log.label}</strong>
                  <small>{log.source === "gateway" ? "Gateway 控制面" : log.workspace_name ?? "工作区运行时"}</small>
                </span>
                <span className={`gateway-log-panel-log-status ${log.status}`}>{logStatusLabel(log)}</span>
              </button>
            ))}
            {!diagnostics && !error ? <span className="gateway-log-panel-empty">正在读取日志入口…</span> : null}
            {diagnostics?.logs.length === 0 ? <span className="gateway-log-panel-empty">当前没有可用日志入口</span> : null}
          </div>
        </aside>

        <article className="gateway-log-panel-viewer" aria-label="Gateway 日志内容">
          {selectedLog ? (
            <>
              <header>
                <div>
                  <strong>{selectedLog.label}</strong>
                  <span>{selectedLog.updated_at ? `更新于 ${formatTime(selectedLog.updated_at)}` : "暂无更新时间"}</span>
                </div>
                <button type="button" className="gateway-log-panel-copy" disabled={!selectedLog.tail} onClick={() => void copySelectedLog()}>
                  <span className="codicon codicon-copy" aria-hidden="true" />复制
                </button>
              </header>
              {selectedLog.status === "unavailable" ? (
                <div className="gateway-log-panel-viewer-empty"><strong>当前无法读取这份日志</strong><span>{selectedLog.error ?? "Gateway 没有返回日志内容。"}</span></div>
              ) : selectedLog.status === "empty" ? (
                <div className="gateway-log-panel-viewer-empty"><strong>日志文件为空</strong><span>服务尚未输出内容，稍后会自动刷新。</span></div>
              ) : (
                <>
                  <div className="gateway-log-panel-raw-filter" role="group" aria-label="原始日志筛选">
                    <button type="button" className={rawFilter === "all" ? "active" : ""} onClick={() => setRawFilter("all")}>全部行</button>
                    <button
                      type="button"
                      title="只筛选当前选中日志中的错误和警告"
                      className={rawFilter === "attention" ? "active" : ""}
                      onClick={() => setRawFilter("attention")}
                    >
                      仅重点行{attentionLines.length ? ` ${attentionLines.length}` : ""}
                    </button>
                  </div>
                  <pre>
                    {rawFilter === "attention"
                      ? (attentionLines.map((line) => line.rawText).join("\n") || "当前日志没有匹配的错误或警告")
                      : (selectedLog.tail || "暂无日志内容")}
                  </pre>
                </>
              )}
            </>
          ) : (
            <div className="gateway-log-panel-viewer-empty"><strong>暂无日志内容</strong><span>请选择一个日志入口查看 Gateway 最新输出。</span></div>
          )}
        </article>
      </div>
      )}
    </section>
  );
}
