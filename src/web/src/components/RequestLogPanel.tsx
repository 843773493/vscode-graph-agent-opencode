import { useMemo, useState } from "react";
import { formatDateTime } from "../utils/format";
import { prettyJson } from "../utils/jsonDisplay";
import {
  buildRequestLogDisplay,
  buildRequestLogKeyFlow,
  buildUpstreamAttemptDisplay,
  buildRequestReplayDisplay,
  normalizeRequestLogJsonForDisplay,
  requestPromptComponentText,
  type RequestPromptComponentDisplay,
  type RequestLogKeyFlow,
  type RequestReplayDisplay,
  type RequestToolDefinitionDisplay,
  type UpstreamAttemptDisplay,
} from "../state/requestLogDisplay";
import type { LLMRequestLogRecord } from "../types/backend";

function RequestLogKeyFlowSummary({
  readSkills,
  customInvokerNames,
  customToolNames,
  customToolResults,
  finalText,
}: RequestLogKeyFlow) {
  const [open, setOpen] = useState(false);
  if (
    readSkills.length === 0 &&
    customInvokerNames.length === 0 &&
    customToolNames.length === 0 &&
    customToolResults.length === 0 &&
    !finalText
  ) {
    return null;
  }

  const flowItems = [
    {
      label: "读取 skill",
      value: readSkills.join("、") || "未检测到",
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
}

function compactCount(value: number): string {
  if (value < 1000) {
    return String(value);
  }
  return `${(value / 1000).toFixed(value < 10_000 ? 1 : 0)}k`;
}

function RequestCalledToolSummary({ toolNames }: { toolNames: string[] }) {
  const [open, setOpen] = useState(false);
  return (
    <section
      className={`request-log-called-tools${open ? " is-open" : ""}`}
    >
      <button
        type="button"
        className="request-log-collapse-summary"
        aria-expanded={open}
        onClick={() => setOpen((current) => !current)}
      >
        <span>本次调用工具</span>
        <span>{toolNames.length} 个</span>
      </button>
      {open ? (
        toolNames.length > 0 ? (
          <div className="request-log-tool-list">
            {toolNames.map((name) => (
              <code key={name} className="request-log-tool-chip called">
                {name}
              </code>
            ))}
          </div>
        ) : (
          <div className="request-log-tool-empty">本次没有调用工具</div>
        )
      ) : null}
    </section>
  );
}

function PromptComponentCard({
  component,
}: {
  component: RequestPromptComponentDisplay;
}) {
  const [open, setOpen] = useState(false);
  return (
    <section
      className={`request-replay-component${open ? " is-open" : ""}`}
    >
      <button
        type="button"
        className="request-log-collapse-summary"
        aria-expanded={open}
        onClick={() => setOpen((current) => !current)}
      >
        <span className="request-replay-component-title">{component.label}</span>
        <span>{component.source}</span>
        <span>{component.operation === "replace" ? "替换快照" : "追加"}</span>
        <span>{component.blockCount} 块</span>
        <span>{compactCount(component.charCount)} 字符</span>
      </button>
      {open ? <pre>{requestPromptComponentText(component)}</pre> : null}
    </section>
  );
}

function ToolDefinitionCard({ tool }: { tool: RequestToolDefinitionDisplay }) {
  const [open, setOpen] = useState(false);
  return (
    <section
      className={`request-replay-tool${open ? " is-open" : ""}`}
    >
      <button
        type="button"
        className="request-log-collapse-summary"
        aria-expanded={open}
        onClick={() => setOpen((current) => !current)}
      >
        <code>{tool.name}</code>
        <span>{tool.schemaFieldCount} 个 schema 顶层字段</span>
        {tool.description ? <span>{tool.description.length} 字符说明</span> : null}
      </button>
      {open ? <pre>{prettyJson(tool.definition)}</pre> : null}
    </section>
  );
}

function ReplayToolsCard({ replay }: { replay: RequestReplayDisplay }) {
  const [open, setOpen] = useState(false);
  return (
    <section
      className={`request-replay-tools${open ? " is-open" : ""}`}
    >
      <button
        type="button"
        className="request-log-collapse-summary"
        aria-expanded={open}
        onClick={() => setOpen((current) => !current)}
      >
        <span className="request-replay-component-title">Tools</span>
        <span>{replay.tools.length} 个</span>
        <span>{compactCount(replay.toolSchemaCharCount)} schema 字符</span>
      </button>
      {open ? (
        <div className="request-replay-tool-list">
          {replay.tools.map((tool, index) => (
            <ToolDefinitionCard key={`${tool.name}-${index}`} tool={tool} />
          ))}
        </div>
      ) : null}
    </section>
  );
}

function RequestReplayCard({ replay }: { replay: RequestReplayDisplay }) {
  const [open, setOpen] = useState(false);
  return (
    <section
      className={`request-log-replay${open ? " is-open" : ""}`}
    >
      <button
        type="button"
        className="request-log-collapse-summary"
        aria-expanded={open}
        onClick={() => setOpen((current) => !current)}
      >
        <span className="request-log-summary-title">请求组成</span>
        <span>{replay.promptComponents.length} 个 Prompt 组成</span>
        <span>{replay.tools.length} 个 Tools</span>
        <span>{compactCount(replay.systemPromptCharCount)} Prompt 字符</span>
        {replay.legacy ? <span className="request-log-legacy">旧日志：来源未记录</span> : null}
      </button>
      {open ? (
        <div className="request-replay-list">
          {replay.promptComponents.map((component, index) => (
            <PromptComponentCard
              key={`${component.source}-${component.label}-${index}`}
              component={component}
            />
          ))}
          <ReplayToolsCard replay={replay} />
        </div>
      ) : null}
    </section>
  );
}

function LazyFullLogJson({ log }: { log: LLMRequestLogRecord }) {
  const [open, setOpen] = useState(false);
  const text = useMemo(
    () => open ? prettyJson(normalizeRequestLogJsonForDisplay(log)) : "",
    [log, open],
  );
  return (
    <section
      className={`panel-detail request-log-json${open ? " is-open" : ""}`}
    >
      <button
        type="button"
        className="request-log-json-summary"
        aria-expanded={open}
        onClick={() => setOpen((current) => !current)}
      >
        完整日志 JSON（连续文本增量合并展示）
      </button>
      {open ? <pre>{text}</pre> : null}
    </section>
  );
}

function UpstreamAttemptCard({
  attempt,
  index,
}: {
  attempt: UpstreamAttemptDisplay;
  index: number;
}) {
  const [open, setOpen] = useState(false);
  return (
    <section className={`request-log-upstream-attempt${open ? " is-open" : ""}`}>
      <button
        type="button"
        className="request-log-collapse-summary"
        aria-expanded={open}
        onClick={() => setOpen((current) => !current)}
      >
        <span className="request-replay-component-title">上游尝试 {index + 1}</span>
        <span>{attempt.callType}</span>
        <span>{attempt.provider}</span>
        <code>{attempt.model}</code>
        {attempt.error ? <span className="request-log-upstream-error">失败</span> : null}
      </button>
      {open ? (
        <div className="request-log-upstream-payloads">
          {attempt.apiBase ? <div className="panel-meta">endpoint: {attempt.apiBase}</div> : null}
          <section>
            <strong>最终上游请求（认证信息已脱敏）</strong>
            <pre>{prettyJson(attempt.request)}</pre>
          </section>
          <section>
            <strong>上游原始响应</strong>
            <pre>{prettyJson(attempt.response)}</pre>
          </section>
          {attempt.error ? (
            <section className="request-log-upstream-error">
              <strong>上游错误</strong>
              <pre>{prettyJson(attempt.error)}</pre>
            </section>
          ) : null}
        </div>
      ) : null}
    </section>
  );
}

function UpstreamExchangeCard({ log }: { log: LLMRequestLogRecord }) {
  const [open, setOpen] = useState(false);
  const attempts = buildUpstreamAttemptDisplay(log);
  return (
    <section className={`request-log-upstream${open ? " is-open" : ""}`}>
      <button
        type="button"
        className="request-log-collapse-summary"
        aria-expanded={open}
        onClick={() => setOpen((current) => !current)}
      >
        <span className="request-log-summary-title">上游实际请求 / 响应</span>
        <span>{attempts.length} 次尝试</span>
        {attempts.length === 0 ? <span className="request-log-legacy">旧日志未采集</span> : null}
      </button>
      {open ? (
        <div className="request-log-upstream-list">
          {attempts.map((attempt, index) => (
            <UpstreamAttemptCard
              key={`${attempt.callType}-${attempt.model}-${index}`}
              attempt={attempt}
              index={index}
            />
          ))}
        </div>
      ) : null}
    </section>
  );
}

function RequestLogCard({
  log,
  chronologicalIndex,
}: {
  log: LLMRequestLogRecord;
  chronologicalIndex: number;
}) {
  const [open, setOpen] = useState(false);
  const display = buildRequestLogDisplay(log);
  const replay = buildRequestReplayDisplay(log);
  const timestamp = formatDateTime(new Date(log.timestamp).toISOString());

  return (
    <article
      className={`panel-card request-log-card${open ? " is-open" : ""}`}
    >
      <button
        type="button"
        className="request-log-card-summary request-log-collapse-summary"
        aria-expanded={open}
        onClick={() => setOpen((current) => !current)}
      >
        <span className="request-log-card-heading">
          <span className="panel-index">第 {chronologicalIndex + 1} 次</span>
          <span className="panel-type">{display.model}</span>
          {timestamp ? <span>{timestamp}</span> : null}
        </span>
        <span className="request-log-card-stats">
          <span>{display.phaseLabel}</span>
          <span>{replay.messageCount} 条消息</span>
          <span>{replay.promptComponents.length} 段 Prompt</span>
          <span>{display.toolNames.length} 个 Tools</span>
          <span>{display.calledToolNames.length} 次调用</span>
        </span>
      </button>

      {open ? (
        <div className="request-log-card-body">
          <div className="panel-meta">
            {log.job_id ? <span>job: {log.job_id}</span> : null}
            <span>file: {log.file_name}</span>
            <span>响应消息: {display.responseMessageCount}</span>
          </div>

          <div className="request-log-summary">
            <div className="request-log-summary-label">响应预览</div>
            <div
              aria-label={
                display.responseText ? `响应预览：${display.responseText}` : undefined
              }
            >
              {display.responseText ||
                (display.phaseLabel === "工具调用请求"
                  ? "模型在本次请求中选择调用工具，因此没有自然语言正文。"
                  : "本次是链路中的中间请求，没有可读自然语言正文。")}
            </div>
          </div>

          <RequestCalledToolSummary toolNames={display.calledToolNames} />
          <RequestReplayCard replay={replay} />
          <UpstreamExchangeCard log={log} />
          <LazyFullLogJson log={log} />
        </div>
      ) : null}
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

  // 请求视图是逐次审计模型输入、输出和请求组成来源的入口。
  // 大体积 Prompt、工具 schema 与 JSON 必须按层级懒展开；不要恢复成默认渲染全部内容，
  // 否则历史中的大量 text_delta 会创建过多 DOM，并再次拖慢会话切换和滚动。
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

      {!error && displayLogs.length > 0 ? (
        <div className="panel-list">
          <RequestLogKeyFlowSummary {...keyFlow} />
          {displayLogs.map((log, index) => (
            <RequestLogCard
              key={`${log.file_path}-${log.timestamp}-${index}`}
              log={log}
              chronologicalIndex={chronologicalIndexes.get(log) ?? index}
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
