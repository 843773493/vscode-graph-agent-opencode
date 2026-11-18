import { t, formatNumber } from "../../../../../shared/core/i18n/index.js";
import React, { useMemo, useState, useLayoutEffect, useRef } from "react";
import type { RequestLogNavigation } from "../../../hooks/runtime/useRequestLogNavigation";
import { formatDateTime } from "../../../utils/format";
import type { RequestLogKeyFlow } from "../../../state/requestLogDisplay/index";
import type { LLMRequestLogSummaryDTO } from "../../../types/backend";
import { useRequestLogDetail, useRequestLogChain } from "../../../hooks/runtime/useRequestLogLoader";
import { useScrollWindow } from "../../eventQueue/useScrollWindow";
import RequestLogCard from "./RequestLogCard";

export const INITIAL_VISIBLE_REQUEST_LOG_COUNT = 10;
export const OLDER_REQUEST_LOG_BATCH_SIZE = 10;

const RequestLogKeyFlowSummary = React.memo(function RequestLogKeyFlowSummary({
  acceptedSkillLoads,
  customInvokerNames,
  customToolNames,
  customToolResults,
  finalText,
}: RequestLogKeyFlow) {
  const [open, setOpen] = useState(true);
  if (
    acceptedSkillLoads.length === 0 &&
    customInvokerNames.length === 0 &&
    customToolNames.length === 0 &&
    customToolResults.length === 0 &&
    !finalText
  ) {
    return null;
  }

  const flowItems = [
    {
      label: t("inspection.request.skillLoads"),
      value: acceptedSkillLoads.join("、") || t("inspection.request.notDetected"),
    },
    {
      label: t("inspection.request.invoker"),
      value: customInvokerNames.join("、") || t("inspection.request.notDetected"),
    },
    {
      label: t("inspection.request.targetTool"),
      value: customToolNames.join("、") || t("inspection.request.notDetected"),
    },
    {
      label: t("inspection.request.toolResults"),
      value:
        customToolResults.length > 0
          ? customToolResults
              .map(
                (result) =>
                  `${result.invocationToolName} -> ${result.toolName} -> ${result.resultText}`,
              )
              .join("；")
          : t("inspection.request.notDetected"),
    },
    {
      label: t("inspection.request.final"),
      value: finalText || t("inspection.request.noFinal"),
    },
  ];

  return (
    <section
      className={`panel-card request-log-key-flow${open ? " is-open" : ""}`}
      aria-label={t("inspection.request.chain")}
    >
      <button
        type="button"
        className="request-log-collapse-summary"
        aria-expanded={open}
        onClick={() => setOpen((current) => !current)}
      >
        <span className="request-log-summary-title">{t("inspection.request.chain")}</span>
        <span className="panel-pill">{t("inspection.request.stages", formatNumber(flowItems.length))}</span>
      </button>
      {open ? (
        <ol className="request-log-key-flow-list">
          {flowItems.map((item, index) => (
            <li key={item.label}>
              <span className="request-log-flow-index">{formatNumber(index + 1)}</span>
              <div>
                <span>{item.label}</span>
                <strong>{item.value}</strong>
              </div>
            </li>
          ))}
        </ol>
      ) : null}
    </section>
  );
});

export default function RequestLogPanel({
  apiPort, workspaceId,
  navigation,
  onCloseNavigation,
  logs,
  loading,
  error,
  loadedAt,
  sessionId,
  scopeKey,
  active,
  onRetryRequestLogs,
}: {
  navigation?: RequestLogNavigation;
  onCloseNavigation?: () => void;
  apiPort: number;
  workspaceId: string | null;
  logs: LLMRequestLogSummaryDTO[];
  loading: boolean;
  error: string | null;
  loadedAt: string | null;
  sessionId: string;
  scopeKey: string;
  active: boolean;
  onRetryRequestLogs: () => void;
}) {
  const { firstVisibleIndex, handleListScroll, listRef } = useScrollWindow({
    active,
    scopeKey,
    itemCount: logs.length,
    initialVisibleCount: INITIAL_VISIBLE_REQUEST_LOG_COUNT,
    olderBatchSize: OLDER_REQUEST_LOG_BATCH_SIZE,
  });
  const renderedLogs = logs.slice(firstVisibleIndex);
  const hasOlderLogs = firstVisibleIndex > 0;
  if (navigation) return <LocatedRequestLog key={`${navigation.match.reference?.file_name}/${navigation.match.reference?.start_codepoint}`} navigation={navigation} onClose={onCloseNavigation} />;
  // 请求视图是逐次审计模型输入、输出和请求组成来源的入口。
  // 大体积 Prompt、工具 schema 与 JSON 必须按层级懒展开；不要恢复成默认渲染全部内容，
  // 否则历史中的大量 text_delta 会创建过多 DOM，并再次拖慢会话切换和滚动。
  return (
    <section className="panel-view request-log-panel" aria-busy={loading}>
      <div className="panel-header">
        <div className="panel-title">{t("inspection.request.title")}</div>
        <div className="panel-header-meta">
          <span>{t("inspection.request.logs", formatNumber(logs.length))}</span>
          <span>{t("inspection.request.shown", formatNumber(renderedLogs.length))}</span>
          <span>{t("inspection.request.order")}</span>
          <span>{sessionId || t("inspection.request.noSession")}</span>
          {loadedAt ? <span>{t("inspection.request.loadedAt", formatDateTime(loadedAt))}</span> : null}
        </div>
      </div>


      {error ? (
        <div className="panel-feedback" role="alert">
          <div className="error-title">{t("inspection.request.loadFailed")}</div>
          <div className="error-message">{error}</div>
          <button
            type="button"
            className="error-retry-button"
            onClick={onRetryRequestLogs}
          >

            {t("inspection.request.retry")}
          </button>
        </div>
      ) : null}

      {logs.length > 0 ? (
        <div
          className="panel-list"
          ref={listRef}
          onScroll={handleListScroll}
        >
          {hasOlderLogs ? (
            <div className="request-log-load-older" role="status">

              {t("inspection.request.scrollOlder")}
            </div>
          ) : null}
          <RequestLogChain key={loadedAt} logs={logs} apiPort={apiPort} workspaceId={workspaceId} active={active} />
          {renderedLogs.map((log, index) => (
            <RequestLogEntry
              key={`${log.file_name}-${loadedAt}`}
              summary={log} apiPort={apiPort} workspaceId={workspaceId} active={active}
              chronologicalIndex={firstVisibleIndex + index}
            />
          ))}
        </div>
      ) : null}

      {!loading && !error && logs.length === 0 ? (
        <div className="empty-state">

          {t("inspection.request.empty")}
        </div>
      ) : null}
    </section>
  );
}


function RequestLogEntry({ summary, apiPort, workspaceId, active, chronologicalIndex }: {
  summary: LLMRequestLogSummaryDTO; apiPort: number; workspaceId: string | null;
  active: boolean; chronologicalIndex: number;
}) {
  const [open, setOpen] = useState(false);
  const { log, error } = useRequestLogDetail(apiPort, workspaceId, summary.session_id, summary.file_name, open && active);
  if (open && active && log) return <RequestLogCard log={log} chronologicalIndex={chronologicalIndex}
    initiallyOpen onClose={() => setOpen(false)} />;
  return <article className={`panel-card request-log-card${open ? " is-open" : ""}`}>
    <button type="button" className="request-log-card-summary request-log-collapse-summary"
      aria-expanded={open} onClick={() => setOpen(!open)}>
      <span className="request-log-card-heading"><span className="panel-index">{t("inspection.request.ordinal", formatNumber(chronologicalIndex + 1))}</span>
        <span>{formatDateTime(new Date(summary.recorded_at).toISOString())}</span></span>
      <span>{t("inspection.request.expandSize", formatNumber(summary.size_bytes / 1024, { minimumFractionDigits: 1, maximumFractionDigits: 1 }))}</span>
    </button>
    {open && error ? <div role="alert">{t("inspection.request.readFailed", error)}
      <button type="button" onClick={() => setOpen(false)}>{t("inspection.request.closeRetry")}</button></div>
      : open ? <div role="status">{t("inspection.request.loadingOne")}</div> : null}
  </article>;
}

function RequestLogChain({ logs, apiPort, workspaceId, active }: {
  logs: LLMRequestLogSummaryDTO[]; apiPort: number; workspaceId: string | null; active: boolean;
}) {
  const { loading, load, flow, error } = useRequestLogChain(logs, apiPort, workspaceId, active);
  if (flow) return <RequestLogKeyFlowSummary {...flow} />;
  return <section className="panel-card request-log-key-flow">
    <button type="button" disabled={loading} onClick={load}>
      {loading ? t("inspection.request.loadingChain") : t("inspection.request.loadChain")}
    </button>
    {error ? <div role="alert">{t("inspection.request.chainFailed", error)}</div> : null}
  </section>;
}

function LocatedRequestLog({ navigation, onClose }: { navigation: RequestLogNavigation; onClose?: () => void }) {
  const mark = useRef<HTMLElement>(null);
  const location = navigation.location;
  useLayoutEffect(() => { mark.current?.scrollIntoView({ block: "center" }); }, [location]);
  const source = useMemo(() => {
    if (!location?.reference || navigation.recordOnly) return null;
    const characters = Array.from(location.text);
    const { start_codepoint: start, end_codepoint: end } = location.reference;
    return { before: characters.slice(Math.max(0, start - 1000), start).join(""),
      match: characters.slice(start, end).join(""), after: characters.slice(end, end + 1000).join(""),
      hasBefore: start > 1000, hasAfter: end + 1000 < characters.length };
  }, [location, navigation.recordOnly]);
  return <section className="panel-view request-log-panel" aria-label={t("inspection.request.locatedAria")}>
    <div className="panel-header"><span>{t("inspection.request.sessionRecord", navigation.match.reference?.file_name)}</span>
      <button type="button" onClick={onClose}>{t("inspection.request.closeLocation")}</button></div>
    {navigation.error ? <div className="empty-state error-state" role="alert">{t("inspection.request.matchFailed", navigation.error)}</div>
      : location ? <div className="panel-list">
        {source && <section aria-label={t("inspection.request.originalAria")}><p>{t("inspection.match.original", formatNumber(location.reference!.start_codepoint), formatNumber(location.reference!.end_codepoint))}</p>
          <pre>{source.hasBefore ? "…" : ""}{source.before}<mark ref={mark}>{source.match}</mark>{source.after}{source.hasAfter ? "…" : ""}</pre></section>}
        <RequestLogCard key={location.record!.file_name} log={location.record!} initiallyOpen />
      </div> : <div className="empty-state" role="status">{t("inspection.request.locating")}</div>}
  </section>;
}
