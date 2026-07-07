import { formatDateTime } from "../utils/format";
import { prettyJson } from "../utils/jsonDisplay";
import {
  buildRequestLogDisplay,
  buildRequestLogKeyFlow,
  type RequestLogKeyFlow,
} from "../state/requestLogDisplay";
import type { LLMRequestLogRecord } from "../types/backend";

function RequestLogKeyFlowSummary({
  readSkills,
  customInvokerNames,
  customToolNames,
  customToolResults,
  finalText,
}: RequestLogKeyFlow) {
  if (
    readSkills.length === 0 &&
    customInvokerNames.length === 0 &&
    customToolNames.length === 0 &&
    customToolResults.length === 0 &&
    !finalText
  ) {
    return null;
  }

  return (
    <div className="request-log-key-flow" aria-label="请求链路">
      <div className="request-log-key-flow-title">请求链路</div>
      <div className="request-log-key-flow-grid">
        <div>
          <span>读取 skill</span>
          <strong>{readSkills.join("、") || "未检测到"}</strong>
        </div>
        <div>
          <span>固定扩展入口</span>
          <strong>{customInvokerNames.join("、") || "未检测到"}</strong>
        </div>
        <div>
          <span>目标扩展工具</span>
          <strong>{customToolNames.join("、") || "未检测到"}</strong>
        </div>
        <div>
          <span>扩展工具结果</span>
          <strong>
            {customToolResults.length > 0
              ? customToolResults
                  .map(
                    (result) =>
                      `${result.invocationToolName} -> ${result.toolName} -> ${result.resultText}`,
                  )
                  .join("；")
              : "未检测到"}
          </strong>
        </div>
        <div>
          <span>最终回复</span>
          <strong aria-label={finalText ? `最终回复：${finalText}` : undefined}>
            {finalText || "暂无最终回复"}
          </strong>
        </div>
      </div>
    </div>
  );
}

function RequestCalledToolSummary({ toolNames }: { toolNames: string[] }) {
  if (toolNames.length === 0) {
    return null;
  }
  return (
    <div className="request-log-called-tools">
      <span className="request-log-summary-label">本次调用工具</span>
      <div className="request-log-tool-list">
        {toolNames.map((name) => (
          <code key={name} className="request-log-tool-chip called">
            {name}
          </code>
        ))}
      </div>
    </div>
  );
}

function RequestToolSummary({ toolNames }: { toolNames: string[] }) {
  return (
    <div className="request-log-tools">
      <div className="request-log-tools-head">
        <span className="request-log-summary-label">可用工具</span>
        <span>{toolNames.length} 个</span>
      </div>
      {toolNames.length > 0 ? (
        <div className="request-log-tool-list">
          {toolNames.map((name) => (
            <code key={name} className="request-log-tool-chip">
              {name}
            </code>
          ))}
        </div>
      ) : (
        <div className="request-log-tool-empty">本次请求未携带工具定义</div>
      )}
    </div>
  );
}

function RequestLogCard({
  log,
  chronologicalIndex,
}: {
  log: LLMRequestLogRecord;
  chronologicalIndex: number;
}) {
  const display = buildRequestLogDisplay(log);
  const timestamp = formatDateTime(new Date(log.timestamp).toISOString());

  return (
    <article className="panel-card request-log-card">
      <div className="panel-card-head">
        <div className="panel-title-row">
          <span className="panel-index">第 {chronologicalIndex + 1} 次</span>
          <span className="panel-type">{display.model}</span>
        </div>
        <div className="panel-time">
          {timestamp ? <span>{timestamp}</span> : null}
        </div>
      </div>

      <div className="panel-meta">
        {log.job_id ? <span>job: {log.job_id}</span> : null}
        <span>{display.phaseLabel}</span>
        <span>file: {log.file_name}</span>
      </div>

      <div className="request-log-summary-grid">
        <div className="request-log-summary">
          <div className="request-log-summary-label">请求预览</div>
          <div>{display.requestText || "无可读请求文本"}</div>
        </div>
        <div className="request-log-summary">
          <div className="request-log-summary-label">响应预览</div>
          <div aria-label={display.responseText ? `响应预览：${display.responseText}` : undefined}>
            {display.responseText ||
              (display.phaseLabel === "工具调用请求"
                ? "模型在本次请求中选择调用工具，因此没有自然语言正文。"
                : "本次是链路中的中间请求，没有可读自然语言正文。")}
          </div>
        </div>
      </div>

      <RequestCalledToolSummary toolNames={display.calledToolNames} />

      <RequestToolSummary toolNames={display.toolNames} />

      <details className="panel-detail">
        <summary>请求 JSON</summary>
        <pre>{prettyJson(log.request)}</pre>
      </details>

      <details className="panel-detail">
        <summary>响应 JSON</summary>
        <pre>{prettyJson(log.response)}</pre>
      </details>

      <details className="panel-detail">
        <summary>完整日志 JSON</summary>
        <pre>{prettyJson(log)}</pre>
      </details>
    </article>
  );
}

export default function RequestLogPanel({
  logs,
  loading,
  error,
  loadedAt,
  sessionId,
}: {
  logs: LLMRequestLogRecord[];
  loading: boolean;
  error: string | null;
  loadedAt: string | null;
  sessionId: string;
}) {
  const displayLogs = [...logs].sort(
    (left, right) =>
      new Date(right.timestamp).getTime() - new Date(left.timestamp).getTime(),
  );
  const chronologicalIndexes = new Map(
    [...logs]
      .sort((left, right) => left.timestamp - right.timestamp)
      .map((log, index) => [log, index]),
  );
  const keyFlow = buildRequestLogKeyFlow(logs);

  return (
    <section className="panel-view request-log-panel">
      <div className="panel-header">
        <div className="panel-title">请求视图</div>
        <div className="panel-header-meta">
          <span>{logs.length} 条请求响应日志</span>
          <span>最新优先，编号按真实请求顺序</span>
          <span>{sessionId || "无会话"}</span>
          {loadedAt ? <span>读取于 {formatDateTime(loadedAt)}</span> : null}
        </div>
      </div>

      {loading ? <div className="empty-state">正在读取 LLM 请求响应日志...</div> : null}
      {error ? <div className="empty-state">LLM 请求响应日志加载失败：{error}</div> : null}

      {!loading && !error && displayLogs.length > 0 ? (
        <>
          <RequestLogKeyFlowSummary {...keyFlow} />
          <div className="panel-list">
            {displayLogs.map((log, index) => (
              <RequestLogCard
                key={`${log.file_path}-${log.timestamp}-${index}`}
                log={log}
                chronologicalIndex={chronologicalIndexes.get(log) ?? index}
              />
            ))}
          </div>
        </>
      ) : null}

      {!loading && !error && logs.length === 0 ? (
        <div className="empty-state">
          当前会话还没有 .boxteam/logs/llm_requests 记录
        </div>
      ) : null}
    </section>
  );
}
