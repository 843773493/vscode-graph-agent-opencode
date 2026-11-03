import React, { useMemo, useState, useLayoutEffect, useRef } from "react";
import type { RequestLogNavigation } from "../../../hooks/runtime/useRequestLogNavigation";
import { formatDateTime } from "../../../utils/format";
import {
  buildRequestLogKeyFlowFromOrderedLogs,
  type RequestLogKeyFlow,
} from "../../../state/requestLogDisplay/index";
import type { LLMRequestLogRecord } from "../../../types/backend";
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
  const [open, setOpen] = useState(false);
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
      label: "已接受的 Skill 加载",
      value: acceptedSkillLoads.join("、") || "未检测到",
    },
    {
      label: "固定扩展入口",
      value: customInvokerNames.join("、") || "未检测到",
    },
    {
      label: "目标扩展工具",
      value: customToolNames.join("、") || "未检测到",
    },
    {
      label: "扩展工具结果",
      value:
        customToolResults.length > 0
          ? customToolResults
              .map(
                (result) =>
                  `${result.invocationToolName} -> ${result.toolName} -> ${result.resultText}`,
              )
              .join("；")
          : "未检测到",
    },
    {
      label: "最终回复",
      value: finalText || "暂无最终回复",
    },
  ];

  return (
    <section
      className={`panel-card request-log-key-flow${open ? " is-open" : ""}`}
      aria-label="请求链路"
    >
      <button
        type="button"
        className="request-log-collapse-summary"
        aria-expanded={open}
        onClick={() => setOpen((current) => !current)}
      >
        <span className="request-log-summary-title">请求链路</span>
        <span className="panel-pill">{flowItems.length} 个阶段</span>
      </button>
      {open ? (
        <ol className="request-log-key-flow-list">
          {flowItems.map((item, index) => (
            <li key={item.label}>
              <span className="request-log-flow-index">{index + 1}</span>
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
  logs: LLMRequestLogRecord[];
  loading: boolean;
  error: string | null;
  loadedAt: string | null;
  sessionId: string;
  scopeKey: string;
  active: boolean;
  onRetryRequestLogs: () => void;
}) {
  const { displayLogs, keyFlow } = useMemo(() => {
    const displayLogs = [...logs].sort(
      (left, right) => left.timestamp - right.timestamp,
    );
    return {
      displayLogs,
      keyFlow: buildRequestLogKeyFlowFromOrderedLogs(displayLogs),
    };
  }, [logs]);
  const { firstVisibleIndex, handleListScroll, listRef } = useScrollWindow({
    active,
    scopeKey,
    itemCount: displayLogs.length,
    initialVisibleCount: INITIAL_VISIBLE_REQUEST_LOG_COUNT,
    olderBatchSize: OLDER_REQUEST_LOG_BATCH_SIZE,
  });
  const renderedLogs = displayLogs.slice(firstVisibleIndex);
  const hasOlderLogs = firstVisibleIndex > 0;
  if (navigation) return <LocatedRequestLog key={`${navigation.match.reference?.file_name}/${navigation.match.reference?.start_codepoint}`} navigation={navigation} onClose={onCloseNavigation} />;
  // 请求视图是逐次审计模型输入、输出和请求组成来源的入口。
  // 大体积 Prompt、工具 schema 与 JSON 必须按层级懒展开；不要恢复成默认渲染全部内容，
  // 否则历史中的大量 text_delta 会创建过多 DOM，并再次拖慢会话切换和滚动。
  return (
    <section className="panel-view request-log-panel">
      <div className="panel-header">
        <div className="panel-title">请求视图</div>
        <div className="panel-header-meta">
          <span>{logs.length} 条请求响应日志</span>
          <span>已显示 {renderedLogs.length}</span>
          <span>早期在上，最新在下</span>
          <span>{sessionId || "无会话"}</span>
          {loadedAt ? <span>读取于 {formatDateTime(loadedAt)}</span> : null}
        </div>
      </div>

      {loading ? <div className="empty-state">正在读取 LLM 请求响应日志...</div> : null}
      {error ? (
        <div className="empty-state error-state" role="alert">
          <div className="error-title">LLM 请求响应日志加载失败</div>
          <div className="error-message">{error}</div>
          <button
            type="button"
            className="error-retry-button"
            onClick={onRetryRequestLogs}
          >
            重新读取请求日志
          </button>
        </div>
      ) : null}

      {!error && displayLogs.length > 0 ? (
        <div
          className="panel-list"
          ref={listRef}
          onScroll={handleListScroll}
        >
          {hasOlderLogs ? (
            <div className="request-log-load-older" role="status">
              向上滚动加载更早请求
            </div>
          ) : null}
          <RequestLogKeyFlowSummary {...keyFlow} />
          {renderedLogs.map((log, index) => (
            <RequestLogCard
              key={`${log.file_name}-${log.timestamp}-${index}`}
              log={log}
              chronologicalIndex={firstVisibleIndex + index}
            />
          ))}
        </div>
      ) : null}

      {!loading && !error && logs.length === 0 ? (
        <div className="empty-state">
          当前会话还没有 .boxteam/logs/llm_requests 记录
        </div>
      ) : null}
    </section>
  );
}


function LocatedRequestLog({ navigation, onClose }: { navigation: RequestLogNavigation; onClose?: () => void }) {
  const mark = useRef<HTMLElement>(null);
  const location = navigation.location;
  useLayoutEffect(() => { mark.current?.scrollIntoView({ block: "center" }); }, [location]);
  const source = useMemo(() => {
    if (!location?.reference) return null;
    const characters = Array.from(location.text);
    const { start_codepoint: start, end_codepoint: end } = location.reference;
    return { before: characters.slice(Math.max(0, start - 1000), start).join(""),
      match: characters.slice(start, end).join(""), after: characters.slice(end, end + 1000).join(""),
      hasBefore: start > 1000, hasAfter: end + 1000 < characters.length };
  }, [location]);
  return <section className="panel-view request-log-panel" aria-label="请求日志搜索定位">
    <div className="panel-header"><span>会话级 LLM 请求记录 · {navigation.match.reference?.file_name}</span>
      <button type="button" onClick={onClose}>关闭请求定位</button></div>
    {navigation.error ? <div className="empty-state error-state" role="alert">请求日志命中无法打开：{navigation.error}</div>
      : location && source ? <div className="panel-list">
        <section aria-label="请求日志命中原文"><p>命中原文（Unicode 码点 {location.reference!.start_codepoint}–{location.reference!.end_codepoint}；相邻上下文最多各 1000 字符）</p>
          <pre>{source.hasBefore ? "…" : ""}{source.before}<mark ref={mark}>{source.match}</mark>{source.after}{source.hasAfter ? "…" : ""}</pre></section>
        <RequestLogCard key={location.record!.file_name} log={location.record!} initiallyOpen />
      </div> : <div className="empty-state" role="status">正在核验并读取命中的请求记录…</div>}
  </section>;
}
