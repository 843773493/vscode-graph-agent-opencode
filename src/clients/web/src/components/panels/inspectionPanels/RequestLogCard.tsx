import { t, formatNumber } from "../../../../../shared/core/i18n/index.js";
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
    return formatNumber(value);
  }
  return `${formatNumber(value / 1000, { minimumFractionDigits: value < 10_000 ? 1 : 0, maximumFractionDigits: value < 10_000 ? 1 : 0 })}k`;
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
        <span>{t("inspection.request.calledTools")}</span>
        <span>{t("inspection.request.count", formatNumber(toolNames.length))}</span>
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
          <div className="request-log-tool-empty">{t("inspection.request.noCalledTools")}</div>
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
        <span>{component.operation === "replace" ? t("inspection.request.replaceSnapshot") : t("inspection.request.append")}</span>
        <span>{t("inspection.request.blocks", formatNumber(component.blockCount))}</span>
        <span>{t("inspection.request.characters", compactCount(component.charCount))}</span>
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
        <span>{t("inspection.request.schemaFields", formatNumber(tool.schemaFieldCount))}</span>
        {tool.description ? <span>{t("inspection.request.descriptionCharacters", formatNumber(tool.description.length))}</span> : null}
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
        <span>{t("inspection.request.count", formatNumber(replay.tools.length))}</span>
        <span>{t("inspection.request.schemaCharacters", compactCount(replay.toolSchemaCharCount))}</span>
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
        <span className="request-log-summary-title">{t("inspection.request.composition")}</span>
        <span>{t("inspection.request.promptComponents", formatNumber(replay.promptComponents.length))}</span>
        <span>{t("inspection.request.tools", formatNumber(replay.tools.length))}</span>
        <span>{t("inspection.request.promptCharacters", compactCount(replay.systemPromptCharCount))}</span>
        {replay.legacy ? <span className="request-log-legacy">{t("inspection.request.legacySource")}</span> : null}
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

        {t("inspection.request.fullJson")}
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
        <span className="request-replay-component-title">{t("inspection.request.attempt", formatNumber(index + 1))}</span>
        <span>{attempt.callType}</span>
        <span>{attempt.provider}</span>
        <code>{attempt.model}</code>
        {attempt.error ? <span className="request-log-upstream-error">{t("inspection.request.failed")}</span> : null}
      </button>
      {open ? (
        <div className="request-log-upstream-payloads">
          {attempt.apiBase ? <div className="panel-meta">endpoint: {attempt.apiBase}</div> : null}
          <section>
            <strong>{t("inspection.request.upstreamRequest")}</strong>
            <pre>{prettyJson(attempt.request)}</pre>
          </section>
          <section>
            <strong>{t("inspection.request.upstreamResponse")}</strong>
            <pre>{prettyJson(attempt.response)}</pre>
          </section>
          {attempt.error ? (
            <section className="request-log-upstream-error">
              <strong>{t("inspection.request.upstreamError")}</strong>
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
        <span className="request-log-summary-title">{t("inspection.request.upstream")}</span>
        <span>{t("inspection.request.attempts", formatNumber(attemptCount))}</span>
        {attemptCount === 0 ? <span className="request-log-legacy">{t("inspection.request.legacyUpstream")}</span> : null}
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
  initiallyOpen = false,
  onClose,
}: {
  log: LLMRequestLogRecord;
  chronologicalIndex?: number;
  initiallyOpen?: boolean;
  onClose?: () => void;
}) {
  const [open, setOpen] = useState(initiallyOpen);
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
        onClick={() => { if (open && onClose) onClose(); else setOpen(!open); }}
      >
        <span className="request-log-card-heading">
          <span className="panel-index">{chronologicalIndex === undefined ? log.file_name : t("inspection.request.ordinal", formatNumber(chronologicalIndex + 1))}</span>
          <span className="panel-type">{display.model}</span>
          {timestamp ? <span>{timestamp}</span> : null}
        </span>
        <span className="request-log-card-stats">
          <span>{t(`inspection.request.phase.${display.phase}`)}</span>
          <span>{t("inspection.request.messages", formatNumber(replay.messageCount))}</span>
          <span>{t("inspection.request.promptSegments", formatNumber(replay.promptComponents.length))}</span>
          <span>{t("inspection.request.tools", formatNumber(display.toolNames.length))}</span>
          <span>{t("inspection.request.calls", formatNumber(display.calledToolNames.length))}</span>
        </span>
      </button>

      {open ? (
        <div className="request-log-card-body">
          <div className="panel-meta">
            {log.job_id ? <span>job: {log.job_id}</span> : null}
            <span>file: {log.file_name}</span>
            <span>{t("inspection.request.responseMessages", formatNumber(display.responseMessageCount))}</span>
          </div>

          <div className="request-log-summary">
            <div className="request-log-summary-label">{t("inspection.request.preview")}</div>
            <div
              aria-label={
                display.responseText ? t("inspection.request.previewAria", display.responseText) : undefined
              }
            >
              {display.responseText ||
                (display.phase === "tool_call"
                  ? t("inspection.request.toolCallEmpty")
                  : t("inspection.request.intermediateEmpty"))}
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
