import { useCallback, useEffect, useMemo, useState } from "react";
import { getGatewayDiagnostics } from "../gatewayApi";
import type { GatewayDiagnosticLog, GatewayDiagnostics } from "../types/backend";
import {
  diagnosticLogStatusLabel,
  diagnosticLogUnavailableHint,
} from "./gatewayLogPresentation";

interface GatewayLogPanelProps {
  apiPort: number;
  workspaceId: string | null;
  height: number;
  onClose: () => void;
  onOpenTerminal?: () => void;
  onOpenPorts?: () => void;
  onOpenAutomation?: () => void;
}

function formatTime(value: string | null): string {
  if (!value) return "暂无时间";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
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

function serviceIcon(log: GatewayDiagnosticLog): string {
  if (log.service.includes("terminal")) return "codicon-terminal";
  if (log.service.includes("browser")) return "codicon-globe";
  return "codicon-server-process";
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
      if (!silent) setLoading(true);
      setError(null);
      try {
        const next = await getGatewayDiagnostics(apiPort, {
          workspaceId,
          logId,
          tailLines: 300,
        });
        const nextWorkspaceLogs = getWorkspaceLogs(next, workspaceId);
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
    },
    [apiPort, workspaceId],
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

  const copySelectedLog = async () => {
    if (!selectedLog?.tail) return;
    try {
      await navigator.clipboard.writeText(selectedLog.tail);
    } catch (copyError) {
      setError(`复制失败：${copyError instanceof Error ? copyError.message : String(copyError)}`);
    }
  };

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
        <aside className="gateway-log-panel-list" aria-label="当前工作区日志入口">
          <div className="gateway-log-panel-list-heading">
            <strong>输出通道</strong>
            <span>{workspaceLogs.length}</span>
          </div>
          <div className="gateway-log-panel-list-scroll">
            {workspaceLogs.map((log) => (
              <button
                type="button"
                key={log.log_id}
                className={log.log_id === selectedLog?.log_id ? "active" : ""}
                title={`打开日志：${serviceLabel(log)}`}
                aria-label={`打开日志：${serviceLabel(log)}`}
                onClick={() => void loadDiagnostics(log.log_id)}
              >
                <span className={`codicon ${serviceIcon(log)}`} aria-hidden="true" />
                <span className="gateway-log-panel-list-copy">
                  <strong>{serviceLabel(log)}</strong>
                  <small>{log.label}</small>
                </span>
                <span className={`gateway-log-panel-log-status ${log.status}`}>{diagnosticLogStatusLabel(log)}</span>
              </button>
            ))}
            {!diagnostics && !error ? <span className="gateway-log-panel-empty">正在读取输出通道…</span> : null}
            {diagnostics && workspaceLogs.length === 0 ? (
              <span className="gateway-log-panel-empty">当前工作区暂无输出日志</span>
            ) : null}
          </div>
        </aside>

        <article className="gateway-log-panel-viewer" aria-label="当前工作区日志内容">
          {selectedLog ? (
            <>
              <header>
                <div>
                  <strong>{serviceLabel(selectedLog)}</strong>
                  <span>{selectedLog.updated_at ? `更新于 ${formatTime(selectedLog.updated_at)}` : "暂无更新时间"}</span>
                </div>
                <button
                  type="button"
                  className="gateway-log-panel-copy"
                  title="复制当前输出"
                  aria-label="复制当前输出"
                  disabled={!selectedLog.tail}
                  onClick={() => void copySelectedLog()}
                >
                  <span className="codicon codicon-copy" aria-hidden="true" />
                </button>
              </header>
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
                <pre>{selectedLog.tail || "暂无日志内容"}</pre>
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
