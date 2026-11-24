import { boundedFailureText, failureMessage, formatFailureDiagnostic } from "@boxteam/plugin-sdk/failures";
import { HttpRequestError } from "../api/http";
import { failureSummary } from "../../../shared/core/failurePresentation";
import { RealtimeTransportError } from "../../../../shared/realtime.js";

/** 仅在错误已经交给可见出口后使用；保留原异常供调用方核查。 */
export class ReportedError extends Error {
  constructor(message: string, readonly cause?: unknown) {
    super(message);
    this.name = "ReportedError";
  }
}

/** 复合失败已经整体展示，errors 仍保存每一项原异常。 */
export class ReportedAggregateError extends AggregateError {}

export function isErrorReported(error: unknown): boolean {
  return error instanceof ReportedError
    || error instanceof ReportedAggregateError
    || (error instanceof AggregateError && error.errors.length > 0
      && error.errors.every(isErrorReported));
}

/** 把未知抛出物归一成可展示的错误文本，供各 hook 与组件复用。 */
export function errorMessage(error: unknown): string {
  return failureMessage(error);
}

/** 只用于默认展示，不能写回协议、业务状态或持久历史。 */
export function errorSummary(error: unknown, operation?: string): string {
  return failureSummary(errorMessage(error), operation);
}

/** 只复制已解析的安全错误字段；不序列化 Response、请求、配置或任意 detail。 */
export function errorDiagnostic(error: unknown): string {
  const parts = [formatFailureDiagnostic(error, { maxBytes: 48 * 1024 })];
  let remaining = 16 * 1024 - 16;
  const pending = [{ error, depth: 0 }];
  const seen = new Set<unknown>();
  while (pending.length && seen.size < 9) {
    const node = pending.shift()!;
    const current = node.error;
    if (seen.has(current)) continue;
    seen.add(current);
    if (current instanceof Error && remaining > 128) {
      const fields: string[] = [];
      // 原 adapter 的机器代码和请求身份独立于 message，默认摘要不携带这些字段。
      if ("code" in current && typeof current.code === "string") fields.push(`code: ${boundedFailureText(current.code, { maxBytes: 256 })}`);
      if ("requestId" in current && typeof current.requestId === "string" && current.requestId) {
        fields.push(`request_id: ${boundedFailureText(current.requestId, { maxBytes: 256 })}`);
      }
      if (current instanceof RealtimeTransportError) {
        if (current.subscriptionId !== undefined) fields.push(`subscription_id: ${current.subscriptionId}`);
        if (current.path) fields.push(`path: ${boundedFailureText(current.path, { maxBytes: 2048 })}`);
      }
      if (current instanceof HttpRequestError) {
        fields.push(`HTTP: ${current.status} ${boundedFailureText(current.statusText, { maxBytes: 128 })}`,
          `path: ${boundedFailureText(current.path, { maxBytes: 2048 })}`);
        if (current.detail && typeof current.detail === "object") {
          const detail = current.detail as Record<string, unknown>;
          for (const key of ["code", "owner", "reason"] as const) {
            const value = detail[key];
            if (typeof value === "string") fields.push(`${key}: ${boundedFailureText(value, { maxBytes: 256 })}`);
          }
          const details = detail.details;
          if (details && typeof details === "object") {
            const known = details as Record<string, unknown>;
            if (typeof known.source_uri === "string") fields.push(`source_uri: ${boundedFailureText(known.source_uri, { maxBytes: 2048 })}`);
            if (Array.isArray(known.issues)) for (const issue of known.issues.slice(0, 32)) {
              if (!issue || typeof issue !== "object") continue;
              const item = issue as Record<string, unknown>;
              if (typeof item.message !== "string") continue;
              const path = Array.isArray(item.path) ? item.path.slice(0, 32).filter(part => typeof part === "string" || typeof part === "number")
                .map(part => typeof part === "string" ? boundedFailureText(part, { maxBytes: 256 }) : part) : [];
              fields.push(`issue ${JSON.stringify(path)}${item.path_exact === true ? ' (exact)' : ''}: ${boundedFailureText(item.message, { maxBytes: 1024 })}`);
            }
          }
        }
        if (current.contextDiagnostic) fields.push(current.contextDiagnostic);
        if (current.bodyDiagnostic) fields.push(current.bodyDiagnostic);
      }
      // HTTP 与 DAP 原 adapter 已持有的来源独立于 message；只投影约定字段。
      const context = "context" in current ? current.context : undefined;
      if (context && typeof context === "object") {
        for (const key of ["stage", "error_type"] as const) {
          const value = (context as Record<string, unknown>)[key];
          if (typeof value === "string") fields.push(`${key}: ${boundedFailureText(value, { maxBytes: 256 })}`);
        }
        for (const key of ["source", "target"] as const) {
          const values = (context as Record<string, unknown>)[key];
          if (!values || typeof values !== "object") continue;
          let count = 0;
          for (const field in values) {
            if (++count > 16) { fields.push(`${key}: …[已截断]`); break; }
            const value = (values as Record<string, unknown>)[field];
            if (typeof value === "string") fields.push(`${key}.${boundedFailureText(field, { maxBytes: 128 })}: ${boundedFailureText(value, { maxBytes: 256 })}`);
          }
        }
      }
      const part = boundedFailureText(fields.join("\n"), { maxBytes: remaining });
      parts.push(part);
      remaining -= new TextEncoder().encode(part).length + 1;
    }
    if (node.depth < 4) {
      if (current instanceof Error && current.cause !== undefined) pending.push({ error: current.cause, depth: node.depth + 1 });
      if (current instanceof AggregateError) for (const child of current.errors.slice(0, Math.max(0, 9 - seen.size - pending.length))) {
        pending.push({ error: child, depth: node.depth + 1 });
      }
    }
  }
  return parts.join("\n");
}
