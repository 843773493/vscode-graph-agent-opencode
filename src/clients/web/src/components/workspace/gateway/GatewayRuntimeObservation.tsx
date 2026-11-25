import { failureSummary } from "../../../../../shared/core/failurePresentation";
import { t } from "../../../../../shared/core/i18n/index.js";
import type { GatewayDiagnostics } from "../../../types/backend";
import { formatByteSize } from "../../../utils/format";

/** 仅展示本次诊断回执，不启动采集、不汇总其它实例或补读请求正文。 */
export default function GatewayRuntimeObservation({ observation }: { observation: NonNullable<GatewayDiagnostics["observation"]> }) {
  const state = (enabled: boolean | undefined) => t(enabled === undefined ? "gateway.observation.unknown"
    : enabled ? "gateway.observation.enabled" : "gateway.observation.disabled");
  return <div className="gateway-runtime-observation">
    <p>{t("gateway.observation.instance", observation.instance_id, observation.applied_revision)}</p>
    <p>{t("gateway.observation.telemetry", state(observation.telemetry?.enabled))}
      {observation.telemetry ? ` · ${t("gateway.observation.sampling", observation.telemetry.sample_ratio,
        observation.samples.length, observation.telemetry.sample_buffer_limit)}` : null}</p>
    <p>{t("gateway.observation.statistics", state(observation.statistics?.enabled))}
      {observation.statistics ? ` · ${t("gateway.observation.interval", observation.statistics.collection_interval_ms)}` : null}</p>
    {observation.requests ? <>
      <p>{t("gateway.observation.requests", observation.requests.count, observation.requests.errors, observation.requests.duration_ms)}</p>
      <p>{t("gateway.observation.proxyRequests", observation.requests.proxy_count, observation.requests.proxy_errors,
        observation.requests.proxy_duration_ms)}</p>
    </> : <p>{t("gateway.observation.requestsUnavailable")}</p>}
    {observation.processes.map(process => <div key={`${process.service}:${process.pid}`}>
      <p>{process.service} · PID {process.pid} · {t("gateway.observation.resources",
        process.rss_bytes == null ? t("gateway.observation.unknown") : formatByteSize(process.rss_bytes),
        process.cpu_seconds == null ? t("gateway.observation.unknown") : process.cpu_seconds)}</p>
      {process.error ? <p className="failure-summary" role="alert">{failureSummary(process.error)}</p> : null}
    </div>)}
    {observation.collection_error ? <p className="failure-summary" role="alert">{failureSummary(observation.collection_error)}</p> : null}
    {observation.unsupported_metrics.length ? <p>{t("gateway.observation.unsupported", observation.unsupported_metrics.join(", "))}</p> : null}
  </div>;
}
