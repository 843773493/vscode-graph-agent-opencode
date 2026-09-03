import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { getGatewayDiagnostics } from "../gatewayApi";
import type { GatewayDiagnosticLog, GatewayDiagnostics } from "../types/backend";
import { diagnosticLogUnavailableHint } from "./gatewayLogPresentation";

interface GatewayLogPanelProps {
  apiPort: number;
  workspaceId: string | null;
  height: number;
  onClose: () => void;
  onOpenTerminal?: () => void;
  onOpenPorts?: () => void;
  onOpenAutomation?: () => void;
}

function serviceLabel(log: GatewayDiagnosticLog): string {
  const labels: Record<string, string> = {
    browser: "浏览器服务",
    browser_manager: "浏览器服务",
    terminal: "终端服务",
    terminal_manager: "终端服务",
  };
  return labels[log.service] ?? log.service.replaceAll("_", " ");
}

function getWorkspaceLogs(
  diagnostics: GatewayDiagnostics | null,
  fallbackWorkspaceId: string | null,
): GatewayDiagnosticLog[] {
  if (!diagnostics) return [];
  const selectedWorkspaceId = diagnostics.selected_workspace_id ?? fallbackWorkspaceId;
  return diagnostics.logs.filter(
    (log) => log.source === "workspace" && log.workspace_id === selectedWorkspaceId,
  );
}

function filterLogTail(tail: string, filterText: string): string {
  const filters = filterText
    .split(",")
    .map((filter) => filter.trim().toLocaleLowerCase())
    .filter(Boolean);
  if (filters.length === 0) return tail;

  const includeFilters = filters.filter((filter) => !filter.startsWith("!"));
  const excludeFilters = filters
    .filter((filter) => filter.startsWith("!"))
    .map((filter) => filter.slice(1).trim())
    .filter(Boolean);

  return tail
    .split("\n")
    .filter((line) => {
      const normalizedLine = line.toLocaleLowerCase();
      if (excludeFilters.some((filter) => normalizedLine.includes(filter))) return false;
      return includeFilters.length === 0 || includeFilters.some((filter) => normalizedLine.includes(filter));
    })
    .join("\n");
}

export default function GatewayLogPanel({
  apiPort,
  workspaceId,
  height,
  onClose,
  onOpenTerminal,
  onOpenPorts,
  onOpenAutomation,
}: GatewayLogPanelProps) {
  const [selectedLogId, setSelectedLogId] = useState<string | null>(null);
  const [diagnostics, setDiagnostics] = useState<GatewayDiagnostics | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [filterText, setFilterText] = useState("");
  const inFlightRequestRef = useRef<{
    key: string;
    promise: Promise<void>;
  } | null>(null);

  const workspaceLogs = useMemo(
    () => getWorkspaceLogs(diagnostics, workspaceId).filter((log) => log.service !== "workspace_api"),
    [diagnostics, workspaceId],
  );
  const selectedLog = useMemo(
    () => workspaceLogs.find((log) => log.log_id === selectedLogId) ?? workspaceLogs[0] ?? null,
    [selectedLogId, workspaceLogs],
  );

  const loadDiagnostics = useCallback(
    async (logId: string | null, silent = false) => {
      if (!workspaceId) {
        setDiagnostics(null);
        setSelectedLogId(null);
        setError("当前会话没有绑定工作区");
        return;
      }
      const requestKey = `${apiPort}:${workspaceId}:${logId ?? ""}`;
      const inFlight = inFlightRequestRef.current;
      if (inFlight?.key === requestKey) {
        await inFlight.promise;
        return;
      }
      if (!silent) setLoading(true);
      setError(null);
      const request = (async () => {
        try {
          const next = await getGatewayDiagnostics(apiPort, {
            workspaceId,
            logId,
            tailLines: 300,
          });
          const nextWorkspaceLogs = getWorkspaceLogs(next, workspaceId).filter(
            (log) => log.service !== "workspace_api",
          );
          setDiagnostics(next);
          setSelectedLogId(
            nextWorkspaceLogs.some((log) => log.log_id === next.selected_log_id)
              ? next.selected_log_id
              : nextWorkspaceLogs[0]?.log_id ?? null,
          );
        } catch (loadError) {
          setError(loadError instanceof Error ? loadError.message : String(loadError));
          if (!silent) {
            setDiagnostics(null);
            setSelectedLogId(null);
          }
        } finally {
          if (!silent) setLoading(false);
        }
      })();
      inFlightRequestRef.current = { key: requestKey, promise: request };
      await request;
      if (inFlightRequestRef.current?.promise === request) {
        inFlightRequestRef.current = null;
      }
    },
    [apiPort, workspaceId],
  );

  const filteredLogTail = useMemo(
    () => (selectedLog?.tail ? filterLogTail(selectedLog.tail, filterText) : ""),
    [filterText, selectedLog],
  );

  useEffect(() => {
    void loadDiagnostics(null);
  }, [loadDiagnostics]);

  useEffect(() => {
    const intervalId = window.setInterval(() => {
      void loadDiagnostics(selectedLogId, true);
    }, 3000);
    return () => window.clearInterval(intervalId);
  }, [loadDiagnostics, selectedLogId]);

  return (
    <section
      className="gateway-log-panel"
      style={{ flexBasis: `${height}px` }}
      data-testid="gateway-log-panel"
    >
      <header className="gateway-log-panel-header">
        <div className="gateway-log-panel-tabs" role="tablist" aria-label="底部面板">
          {onOpenTerminal ? (
            <button
              type="button"
              className="gateway-log-panel-tab"
              role="tab"
              aria-selected="false"
              onClick={onOpenTerminal}
            >
              <span className="codicon codicon-terminal" aria-hidden="true" />
              <span>终端</span>
            </button>
          ) : null}
          <button type="button" className="gateway-log-panel-tab active" role="tab" aria-selected="true">
            <span className="codicon codicon-output" aria-hidden="true" />
            <span>输出</span>
          </button>
          {onOpenPorts ? (
            <button
              type="button"
              className="gateway-log-panel-tab"
              role="tab"
              aria-selected="false"
              onClick={onOpenPorts}
            >
              <span className="codicon codicon-server-environment" aria-hidden="true" />
              <span>端口</span>
            </button>
          ) : null}
          {onOpenAutomation ? (
            <button
              type="button"
              className="gateway-log-panel-tab"
              role="tab"
              aria-selected="false"
              onClick={onOpenAutomation}
            >
              <span className="codicon codicon-gear" aria-hidden="true" />
              <span>自动化</span>
            </button>
          ) : null}
        </div>
        <div className="gateway-log-panel-toolbar">
          <label className="gateway-log-panel-filter">
            <span className="visually-hidden">筛选当前输出</span>
            <input
              type="search"
              value={filterText}
              placeholder="筛选器（例如 text、!excludeText）"
              aria-label="筛选当前输出"
              onChange={(event) => setFilterText(event.target.value)}
            />
          </label>
          <label className="gateway-log-panel-channel">
            <span className="visually-hidden">选择输出通道</span>
            <select
              value={selectedLog?.log_id ?? ""}
              aria-label="选择输出通道"
              disabled={workspaceLogs.length === 0}
              onChange={(event) => void loadDiagnostics(event.target.value)}
            >
              {workspaceLogs.length === 0 ? <option value="">暂无输出通道</option> : null}
              {workspaceLogs.map((log) => (
                <option key={log.log_id} value={log.log_id}>
                  {serviceLabel(log)}
                </option>
              ))}
            </select>
          </label>
        </div>
        <div className="gateway-log-panel-actions">
          <button
            type="button"
            className="gateway-log-panel-icon-button"
            title={loading ? "正在刷新工作区输出" : "刷新工作区输出"}
            aria-label={loading ? "正在刷新工作区输出" : "刷新工作区输出"}
            disabled={loading}
            onClick={() => void loadDiagnostics(selectedLogId)}
          >
            <span className={`codicon codicon-refresh${loading ? " codicon-modifier-spin" : ""}`} aria-hidden="true" />
          </button>
          <button
            type="button"
            className="gateway-log-panel-icon-button"
            title="关闭底部面板"
            aria-label="关闭底部面板"
            onClick={onClose}
          >
            <span className="codicon codicon-close" aria-hidden="true" />
          </button>
        </div>
      </header>

      {error ? (
        <div className="gateway-log-panel-alert" role="alert">
          <span className="codicon codicon-error" aria-hidden="true" />
          <span>工作区输出读取失败：{error}</span>
        </div>
      ) : null}

      <div className="gateway-log-panel-content">
        <article className="gateway-log-panel-viewer" aria-label="当前工作区日志内容">
          {selectedLog ? (
            <>
              {selectedLog.status === "unavailable" ? (
                <div className="gateway-log-panel-viewer-empty">
                  <strong>诊断日志暂不可用</strong>
                  <span>{diagnosticLogUnavailableHint(selectedLog)}</span>
                </div>
              ) : selectedLog.status === "empty" ? (
                <div className="gateway-log-panel-viewer-empty">
                  <strong>日志文件为空</strong>
                  <span>服务尚未输出内容，稍后会自动刷新。</span>
                </div>
              ) : (
                <pre>{filteredLogTail || (filterText.trim() ? "没有匹配的输出" : "暂无日志内容")}</pre>
              )}
            </>
          ) : (
            <div className="gateway-log-panel-viewer-empty">
              <strong>{error ? "无法显示工作区输出" : "暂无工作区输出"}</strong>
              <span>{error ? "请刷新后重试。" : "当前工作区的运行日志会显示在这里。"}</span>
            </div>
          )}
        </article>
      </div>
    </section>
  );
}
