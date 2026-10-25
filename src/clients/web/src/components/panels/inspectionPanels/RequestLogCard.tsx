import React, { useMemo, useState } from "react";
import { formatDateTime } from "../../../utils/format";
import { isRecord, prettyJson } from "../../../utils/jsonDisplay";
import {
  buildRequestLogDisplay,
  buildUpstreamAttemptDisplay,
  buildRequestReplayDisplay,
  normalizeRequestLogJsonForDisplay,
  requestPromptComponentText,
  type RequestPromptComponentDisplay,
  type RequestReplayDisplay,
  type RequestToolDefinitionDisplay,
  type UpstreamAttemptDisplay,
} from "../../../state/requestLogDisplay/index";
import type { LLMRequestLogRecord } from "../../../types/backend";

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
  const attemptCount = useMemo(() => {
    if (!isRecord(log.upstream) || !Array.isArray(log.upstream.attempts)) return 0;
    let count = 0;
    for (const attempt of log.upstream.attempts) {
      if (isRecord(attempt)) count += 1;
    }
    return count;
  }, [log]);
  const attempts = useMemo(
    () => open ? buildUpstreamAttemptDisplay(log) : [],
    [log, open],
  );
  return (
    <section className={`request-log-upstream${open ? " is-open" : ""}`}>
      <button
        type="button"
        className="request-log-collapse-summary"
        aria-expanded={open}
        onClick={() => setOpen((current) => !current)}
      >
        <span className="request-log-summary-title">上游实际请求 / 响应</span>
        <span>{attemptCount} 次尝试</span>
        {attemptCount === 0 ? <span className="request-log-legacy">旧日志未采集</span> : null}
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

const RequestLogCard = React.memo(function RequestLogCard({
  log,
  chronologicalIndex,
}: {
  log: LLMRequestLogRecord;
  chronologicalIndex: number;
}) {
  const [open, setOpen] = useState(false);
  const display = useMemo(() => buildRequestLogDisplay(log), [log]);
  const replay = useMemo(() => buildRequestReplayDisplay(log), [log]);
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
});

export default RequestLogCard;
