import { t } from "../../../../../shared/core/i18n/index.js";
import { workspaceOwnerFromKey } from "../../../api/workspaceIdentity";
import type { ReactNode } from "react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { getGatewayDiagnostics } from "../../../gatewayApi";
import type { GatewayDiagnosticLog, GatewayDiagnostics } from "../../../types/backend";
import { diagnosticLogUnavailableHint } from "../gatewayLogPresentation";
import { errorMessage } from "../../../utils/errorMessage";

function serviceLabel(log: GatewayDiagnosticLog): string {
  if (log.service === "browser") return t("bottom.gatewayLog.browserService");
  if (log.service === "terminal" || log.service === "terminal_manager") {
    return t("bottom.gatewayLog.terminalService");
  }
  return log.service.replaceAll("_", " ");
}

function getWorkspaceLogs(
  diagnostics: GatewayDiagnostics | null,
  fallbackWorkspaceId: string | null,
): GatewayDiagnosticLog[] {
  if (!diagnostics) return [];
  const selectedWorkspaceId = diagnostics.selected_workspace_id ?? (fallbackWorkspaceId ? workspaceOwnerFromKey(fallbackWorkspaceId).workspaceId : null);
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

/** 工作区服务日志与筛选，原生宿主负责面板外壳。 */
export function GatewayLogContent({ apiPort, workspaceId, active, renderToolbar }: {
  apiPort: number; workspaceId: string | null;
  active: boolean; renderToolbar: (tools: ReactNode) => ReactNode;
}) {
  const [selectedLogId, setSelectedLogId] = useState<string | null>(null);
  const selectedLogIdRef = useRef(selectedLogId);
  selectedLogIdRef.current = selectedLogId;
  const [diagnostics, setDiagnostics] = useState<GatewayDiagnostics | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [filterText, setFilterText] = useState("");
  const inFlightRequestRef = useRef<{
    key: string;
    promise: Promise<void>;
  } | null>(null);
  // 工作区与输出通道可被连续切换，每次切换都会重发请求。先发后到的旧响应若不被
  // 丢弃，底部输出面板会显示上一次工作区的日志，与当前工作区不符。
  const requestRevisionRef = useRef(0);
  // 上一次渲染的工作区：工作区切换必须在同一提交内作废旧快照，否则新 workspaceId
  // 会继续渲染 diagnostics 里属于旧工作区的 tail。
  const lastWorkspaceIdRef = useRef(workspaceId);

  const workspaceLogs = useMemo(
    () => getWorkspaceLogs(diagnostics, workspaceId).filter((log) => log.service !== "workspace_api"),
    [diagnostics, workspaceId],
  );
  const selectedLog = useMemo(
    () => workspaceLogs.find((log) => log.log_id === selectedLogId) ?? workspaceLogs[0] ?? null,
    [selectedLogId, workspaceLogs],
  );
  // 工作区一变化就作废旧快照：先发后到的旧响应已经被 revision 丢弃，但「新
  // workspaceId 过滤旧 diagnostics」仍会在新响应到达前渲染出旧工作区的 tail。
  // 在同一提交内清空 diagnostics/selectedLogId 并推进 revision，形成空窗期而非假数据。
  if (lastWorkspaceIdRef.current !== workspaceId) {
    lastWorkspaceIdRef.current = workspaceId;
    requestRevisionRef.current += 1;
    inFlightRequestRef.current = null;
    setDiagnostics(null);
    setSelectedLogId(null);
    setError(null);
    setLoading(Boolean(workspaceId));
  }

  const loadDiagnostics = useCallback(
    async (logId: string | null, silent = false) => {
      if (!workspaceId) {
        // 工作区解绑会让任何在途请求作废，避免它的迟到结果重新填回面板。
        requestRevisionRef.current += 1;
        setDiagnostics(null);
        setSelectedLogId(null);
        setError(t("bottom.gatewayLog.noWorkspace"));
        return;
      }
      const requestKey = `${apiPort}:${workspaceId}:${logId ?? ""}`;
      const inFlight = inFlightRequestRef.current;
      if (inFlight?.key === requestKey) {
        await inFlight.promise;
        return;
      }
      const revision = requestRevisionRef.current + 1;
      requestRevisionRef.current = revision;
      if (!silent) setLoading(true);
      setError(null);
      const request = (async () => {
        try {
          const next = await getGatewayDiagnostics(apiPort, {
            workspaceId,
            logId,
            tailLines: 300,
          });
          if (requestRevisionRef.current !== revision) return;
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
          if (requestRevisionRef.current !== revision) return;
          setError(errorMessage(loadError));
          if (!silent) {
            setDiagnostics(null);
            setSelectedLogId(null);
          }
        } finally {
          if (!silent && requestRevisionRef.current === revision) setLoading(false);
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

  const selectedLogTail = selectedLog?.tail;
  const filteredLogTail = useMemo(
    () => (selectedLogTail ? filterLogTail(selectedLogTail, filterText) : ""),
    [filterText, selectedLogTail],
  );

  useEffect(() => {
    setFilterText("");
  }, [loadDiagnostics]);

  useEffect(() => {
    if (active) void loadDiagnostics(selectedLogIdRef.current);
  }, [active, loadDiagnostics]);

  useEffect(() => {
    if (!active) return;
    const intervalId = window.setInterval(() => {
      void loadDiagnostics(selectedLogId, true);
    }, 3000);
    return () => window.clearInterval(intervalId);
  }, [active, loadDiagnostics, selectedLogId]);

  return (
    <>
      {renderToolbar(<>
        <div className="gateway-log-panel-toolbar">
          <label className="gateway-log-panel-filter">
            <span className="visually-hidden">{t("bottom.gatewayLog.filterOutput")}</span>
            <input
              type="search"
              value={filterText}
              placeholder={t("bottom.gatewayLog.filterPlaceholder")}
              aria-label={t("bottom.gatewayLog.filterOutput")}
              onChange={(event) => setFilterText(event.target.value)}
            />
          </label>
          <label className="gateway-log-panel-channel">
            <span className="visually-hidden">{t("bottom.gatewayLog.selectChannel")}</span>
            <select
              value={selectedLog?.log_id ?? ""}
              aria-label={t("bottom.gatewayLog.selectChannel")}
              disabled={workspaceLogs.length === 0}
              onChange={(event) => {
                void loadDiagnostics(event.target.value || null);
              }}
            >
              {workspaceLogs.length === 0 ? <option value="">{t("bottom.gatewayLog.workspaceServiceLogs")}</option> : null}
              {workspaceLogs.map((log) => (
                <option key={log.log_id} value={log.log_id}>
                  {serviceLabel(log)}
                </option>
              ))}
            </select>
          </label>
        </div>
          <button
            type="button"
            className="workbench-bottom-panel-icon-button"
            title={loading ? t("bottom.gatewayLog.refreshingOutput") : t("bottom.gatewayLog.refreshOutput")}
            aria-label={loading ? t("bottom.gatewayLog.refreshingOutput") : t("bottom.gatewayLog.refreshOutput")}
            disabled={loading}
            onClick={() => void loadDiagnostics(selectedLogId)}
          >
            <span className={`codicon codicon-refresh${loading ? " codicon-modifier-spin" : ""}`} aria-hidden="true" />
          </button>
      </>)}

      {error ? (
        <div className="gateway-log-panel-alert" role="alert">
          <span className="codicon codicon-error" aria-hidden="true" />
          <span>{t("bottom.gatewayLog.readFailed", error)}</span>
        </div>
      ) : null}

      <div className="gateway-log-panel-content">
        <article className="gateway-log-panel-viewer" aria-label={t("bottom.gatewayLog.currentContent")}>
          {selectedLog ? (
            <>
              {selectedLog.status === "unavailable" ? (
                <div className="gateway-log-panel-viewer-empty">
                  <strong>{t("bottom.gatewayLog.unavailable")}</strong>
                  <span>{diagnosticLogUnavailableHint(selectedLog)}</span>
                </div>
              ) : selectedLog.status === "empty" ? (
                <div className="gateway-log-panel-viewer-empty">
                  <strong>{t("bottom.gatewayLog.emptyFile")}</strong>
                  <span>{t("bottom.gatewayLog.emptyFileDetail")}</span>
                </div>
              ) : (
                <pre>{filteredLogTail || (filterText.trim() ? t("bottom.gatewayLog.noMatches") : t("bottom.gatewayLog.noLogContent"))}</pre>
              )}
            </>
          ) : (
            <div className="gateway-log-panel-viewer-empty">
              <strong>{error ? t("bottom.gatewayLog.cannotShowOutput") : t("bottom.gatewayLog.noWorkspaceOutput")}</strong>
              <span>{error ? t("bottom.gatewayLog.retryRefresh") : t("bottom.gatewayLog.outputDescription")}</span>
            </div>
          )}
        </article>
      </div>
    </>
  );
}
