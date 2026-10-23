import React, { useMemo, useState } from "react";
import { formatDateTime } from "../../../utils/format";
import {
  buildRequestLogKeyFlow,
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
  logs,
  loading,
  error,
  loadedAt,
  sessionId,
  scopeKey,
  active,
  onRetryRequestLogs,
}: {
  logs: LLMRequestLogRecord[];
  loading: boolean;
  error: string | null;
  loadedAt: string | null;
  sessionId: string;
  scopeKey: string;
  active: boolean;
  onRetryRequestLogs: () => void;
}) {
  const { displayLogs, keyFlow } = useMemo(() => ({
    displayLogs: [...logs].sort(
      (left, right) =>
        new Date(left.timestamp).getTime() - new Date(right.timestamp).getTime(),
    ),
    keyFlow: buildRequestLogKeyFlow(logs),
  }), [logs]);
  const { firstVisibleIndex, handleListScroll, listRef } = useScrollWindow({
    active,
    scopeKey,
    itemCount: displayLogs.length,
    initialVisibleCount: INITIAL_VISIBLE_REQUEST_LOG_COUNT,
    olderBatchSize: OLDER_REQUEST_LOG_BATCH_SIZE,
  });
  const renderedLogs = displayLogs.slice(firstVisibleIndex);
  const hasOlderLogs = firstVisibleIndex > 0;
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
