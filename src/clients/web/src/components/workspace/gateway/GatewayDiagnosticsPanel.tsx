import { t } from "../../../../../shared/core/i18n/index.js";
import { useCallback, useEffect, useRef, useState } from "react";
import { getGatewayDiagnostics, type GatewayLogTarget } from "../../../gatewayApi";
import type { GatewayRequestTarget } from "../../../api/workspaceIdentity";
import type {
  GatewayDiagnostics,
} from "../../../types/backend";
import { formatLocalTimestamp } from "../../../utils/format";
import {
  diagnosticLogStatusLabel,
} from "../../panels/gatewayLogPresentation";
import { errorDiagnostic, errorSummary } from "../../../utils/errorMessage";
import { copyTextToClipboard } from "../../../utils/clipboard";
import GatewayRuntimeObservation from "./GatewayRuntimeObservation";

interface GatewayDiagnosticsPanelProps {
  apiPort: number;
  target?: GatewayRequestTarget;
  onOpenRetention?: (target: GatewayRequestTarget) => Promise<void>;
  onOpenLog: (target: GatewayLogTarget & { log_id: string }) => Promise<void>;
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
  target,
  onOpenRetention,
  onOpenLog,
}: GatewayDiagnosticsPanelProps) {
  const gatewayId = target?.gateway_id;
  const gatewayConnectionId = target?.gateway_connection_id;
  const workspaceId = target?.workspace_id;
  const [diagnostics, setDiagnostics] = useState<GatewayDiagnostics | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [copyNotice, setCopyNotice] = useState<string | null>(null);
  // 诊断范围可被连续切换；只有最后一次发起的响应才允许写入展示态，否则先发后到的
  // 旧范围响应会覆盖新范围，用户看到与当前选择不符的工作区/日志。
  const requestRef = useRef<AbortController | null>(null);
  // 上一次渲染的诊断范围。范围一变化必须在同一提交内作废旧快照：仅取消请求
  // 只能阻止旧响应发布，但「新范围选择 + 旧 diagnostics」仍会让 selectedLog
  // 回退到 diagnostics.logs[0]，在新响应到达前继续渲染旧工作区/旧日志入口的 tail。
  const scopeKey = JSON.stringify([gatewayId, gatewayConnectionId, workspaceId]);
  const lastScopeRef = useRef(scopeKey);
  if (lastScopeRef.current !== scopeKey) {
    lastScopeRef.current = scopeKey;
    requestRef.current?.abort();
    setDiagnostics(null);
    setError(null);
    setLoading(true);
  }

  const loadDiagnostics = useCallback(
    async () => {
      requestRef.current?.abort();
      const controller = new AbortController();
      requestRef.current = controller;
      setLoading(true);
      setError(null);
      try {
        const next = await getGatewayDiagnostics(apiPort, {
          target: gatewayId ? { gateway_id: gatewayId, gateway_connection_id: gatewayConnectionId, workspace_id: workspaceId } : undefined,
          includeTail: false,
          signal: controller.signal,
        });
        if (controller.signal.aborted) return;
        setDiagnostics(next);
      } catch (loadError) {
        if (controller.signal.aborted) return;
        setError(loadError);
      } finally {
        if (!controller.signal.aborted) setLoading(false);
      }
    }, [apiPort, gatewayId, gatewayConnectionId, workspaceId],
  );

  useEffect(() => {
    void loadDiagnostics();
    return () => requestRef.current?.abort();
  }, [loadDiagnostics]);

  const availableLogCount = diagnostics?.logs.filter((log) => log.status === "available").length ?? 0;
  const offlineWorkspaceCount = diagnostics?.workspaces.filter((item) => item.status === "offline").length ?? 0;
  const copyDiagnosticSummary = async () => {
    if (!diagnostics) return;
    // 默认复制只选身份与状态；原始日志仍由用户在单独的“复制”操作中明确取得。
    const summary = {
      gateway_id: diagnostics.gateway_id, gateway_connection_id: diagnostics.gateway_connection_id,
      selected_workspace_id: diagnostics.selected_workspace_id, checked_at: diagnostics.checked_at, status: diagnostics.status,
      processes: diagnostics.processes.map(process => ({ service: process.service, workspace_ids: process.workspace_ids,
        output_mode: process.output_mode, has_error: Boolean(process.error),
        state: process.state ? { pid: process.state.pid, runtime_instance_id: process.state.runtime_instance_id,
          applied_revision: process.state.applied_revision, level: process.state.level, pretty: process.state.pretty } : undefined })),
      logs: diagnostics.logs.map(log => ({ log_id: log.log_id, workspace_id: log.workspace_id, service: log.service,
        status: log.status, truncated: log.truncated, line_count: log.line_count, size_bytes: log.size_bytes, has_error: Boolean(log.error) })),
    };
    try {
      await copyTextToClipboard(JSON.stringify(summary, null, 2));
      setCopyNotice(t("gateway.diagnostics.summaryCopied"));
    } catch (copyError) { setCopyNotice(errorSummary(copyError)); }
  };

  return (
    <div className="gateway-diagnostics-panel" data-testid="gateway-diagnostics-panel">
      <section className="gateway-diagnostics-toolbar" aria-label={t("gateway.diagnostics.scope")}>
        <div className="gateway-diagnostics-toolbar-actions">
          <span className="gateway-diagnostics-readonly"><span className="codicon codicon-lock" aria-hidden="true" />{t("gateway.diagnostics.readOnly")}</span>
          <button type="button" className="gateway-compact-button" disabled={!diagnostics || loading}
            title={t("gateway.diagnostics.copySummaryHint")} onClick={() => void copyDiagnosticSummary()}>
            {t("gateway.diagnostics.copySummary")}
          </button>
          <button type="button" className="gateway-compact-button" disabled={loading} onClick={() => void loadDiagnostics()}>
            <span className={`codicon codicon-refresh${loading ? " codicon-modifier-spin" : ""}`} aria-hidden="true" />
            {loading ? t("gateway.diagnostics.loading") : t("gateway.diagnostics.refreshLogs")}
          </button>
        </div>
      </section>

      <div className="gateway-console-feedback-slot">
        {Boolean(error) ? (
          <div
            className="gateway-console-feedback error"
            role="alert"
          >
            <span className="codicon codicon-error" aria-hidden="true" />
            <strong>{diagnostics ? t("gateway.diagnostics.refreshFailed") : t("gateway.diagnostics.readFailedTitle")}</strong>
            <span className="gateway-console-feedback-message">{errorSummary(error)}</span>
            <button type="button" onClick={() => {
              void copyTextToClipboard(errorDiagnostic(error)).then(() => setCopyNotice(t('gateway.diagnostics.summaryCopied')))
                .catch(cause => setCopyNotice(errorSummary(cause)));
            }}>{t('shell.copyDiagnostic')}</button>
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

          <details className="gateway-diagnostics-runtime">
            <summary>{t("gateway.storage.title")}</summary>
            <p>{t("gateway.storage.controlDirectory")} <code>{diagnostics.control_directory ?? t("gateway.storage.unreported")}</code></p>
            <p>{t("gateway.storage.logDirectory")} <code>{diagnostics.log_directory ?? t("gateway.storage.unreported")}</code></p>
            <p>{t("gateway.storage.pathBoundary")}</p>
            <p>{t("gateway.storage.retentionBoundary")}</p>
            {onOpenRetention ? <button type="button" className="gateway-compact-button" onClick={() => {
              void onOpenRetention({ gateway_id: diagnostics.gateway_id, gateway_connection_id: diagnostics.gateway_connection_id ?? undefined }).catch(cause => setError(cause));
            }}>{t("gateway.storage.retentionSettings")}</button> : null}
          </details>

          <details className="gateway-diagnostics-runtime">
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

          <details className="gateway-diagnostics-runtime">
            <summary>{t("gateway.observation.title")}</summary>
            <p>{t("gateway.observation.scope", diagnostics.gateway_id)}</p>
            {diagnostics.observation ? <GatewayRuntimeObservation observation={diagnostics.observation} />
              : <p>{t("gateway.observation.unavailable")}</p>}
            {diagnostics.plugin_observations.map((plugin, index) => <div key={plugin.instance?.instance_id ?? index}>
              {plugin.instance ? <h4>{plugin.instance.package_id}/{plugin.instance.plugin_id}@{plugin.instance.package_version}
                {` · ${plugin.instance.gateway_id} · ${plugin.instance.workspace_id ?? plugin.instance.instance_scope}`}</h4> : null}
              {plugin.observation ? <GatewayRuntimeObservation observation={plugin.observation} /> : null}
              {plugin.error ? <p role="alert">{plugin.error}</p> : null}
            </div>)}
          </details>

          <section className="gateway-diagnostics-log-list" aria-label={t("gateway.diagnostics.logEntries")}>
            {diagnostics.logs.map(log => <div key={log.log_id}>
              <strong>{log.label}</strong><span>{diagnosticLogStatusLabel(log)}</span>
              <button type="button" disabled={log.status === 'unavailable'} onClick={() => {
                void onOpenLog({ gateway_id: diagnostics.gateway_id, gateway_connection_id: diagnostics.gateway_connection_id ?? undefined,
                  workspace_id: log.source === 'workspace' ? log.workspace_id ?? undefined : undefined, log_id: log.log_id })
                  .catch(cause => setError(cause));
              }}>{t('gateway.settings.openOutput')}</button>
            </div>)}
          </section>
        </>
      ) : (
        Boolean(error) ? (
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
