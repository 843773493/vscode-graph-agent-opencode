/** 异常摘要与有界诊断；不改变控制分类、不写日志。 */
export const MESSAGE_BYTES = 8 * 1024;
export const DIAGNOSTIC_BYTES = 64 * 1024;
export const MAX_FRAMES = 64;
export const MAX_DESCENDANTS = 8;
export const MAX_DEPTH = 4;
const encoder = new TextEncoder();
const bytes = (text: string) => encoder.encode(text).byteLength;
const MARKER = "…[已截断]";

export function redactFailureText(text: string, secrets: readonly string[] = []): string {
  for (const secret of secrets) if (secret) text = text.replaceAll(secret, "[redacted]");
  return text
    .replace(/\b(authorization|proxy-authorization|cookie|set-cookie)\s*[:=]\s*[^\r\n]+/gim, "$1: [redacted]")
    .replace(/\bBearer\s+[^\s,;"']+/gi, "Bearer [redacted]")
    .replace(/(["']?(?:api[_-]?key|access[_-]?token|token|password|secret|client[_-]?secret)["']?\s*[:=]\s*)(["'])(.*?)\2/gi, "$1$2[redacted]$2")
    .replace(/([?&](?:api[_-]?key|access[_-]?token|token|password|secret)=)[^&#\s]+/gi, "$1[redacted]")
    .replace(/(\b(?:api[_-]?key|access[_-]?token|token|password|secret|client[_-]?secret)\s*[:=]\s*)[^\s,;"'&]+/gi, "$1[redacted]")
    .replace(/(\b[a-z][a-z0-9+.-]*:\/\/)[^/\s@]+@/gi, "$1[redacted]@");
}

function clip(text: string, maxBytes: number): string {
  maxBytes = Math.max(0, maxBytes);
  const prefix = encoder.encode(text.slice(0, maxBytes));
  if (text.length <= maxBytes && prefix.length <= maxBytes) return text;
  const marker = encoder.encode(MARKER).subarray(0, maxBytes);
  return new TextDecoder().decode(prefix.subarray(0, Math.max(0, maxBytes - marker.length)), { stream: true })
    + new TextDecoder().decode(marker, { stream: true });
}

export function boundedFailureText(text: string, { secrets = [], maxBytes = MESSAGE_BYTES }:
  { secrets?: readonly string[]; maxBytes?: number } = {}): string {
  return clip(redactFailureText(text, secrets), Math.min(maxBytes, MESSAGE_BYTES));
}

export function failureMessage(error: unknown, { secrets = [], maxBytes = MESSAGE_BYTES }:
  { secrets?: readonly string[]; maxBytes?: number } = {}): string {
  const message = error instanceof Error ? error.message : String(error ?? "");
  return boundedFailureText(message || "未提供错误文本", { secrets, maxBytes });
}

export function formatFailureDiagnostic(error: unknown, { secrets = [], maxBytes = DIAGNOSTIC_BYTES, maxFrames = MAX_FRAMES }:
  { secrets?: readonly string[]; maxBytes?: number; maxFrames?: number } = {}): string {
  let budget = Math.min(maxBytes, DIAGNOSTIC_BYTES);
  let frames = Math.min(maxFrames, MAX_FRAMES);
  let truncated = false;
  const parts: string[] = [];
  const seen = new Set<unknown>();
  const pending: { error: unknown; depth: number; label: string }[] = [{ error, depth: 0, label: "" }];
  function append(text: string) {
    const available = Math.max(0, budget - bytes(MARKER));
    const safe = redactFailureText(text, secrets);
    const selected = clip(safe, available);
    parts.push(selected);
    budget -= bytes(selected);
    truncated ||= selected !== safe;
  }
  while (pending.length && budget > bytes(MARKER)) {
    const current = pending.pop()!;
    if (seen.has(current.error)) { truncated = true; continue; }
    seen.add(current.error);
    const exception = current.error instanceof Error ? current.error : null;
    append(`${current.label}${exception?.name ?? "Error"}: ${failureMessage(current.error, { secrets })}\n`);
    // 不 split 整份 stack；逐行选择，预算耗尽立即停止。
    if (exception?.stack) {
      let start = exception.stack.indexOf("\n") + 1;
      while (start > 0 && start < exception.stack.length) {
        if (frames === 0 || budget <= 128) { truncated = true; break; }
        const end = exception.stack.indexOf("\n", start);
        const line = exception.stack.slice(start, end < 0 ? undefined : end);
        if (/^\s+at\b/.test(line)) { append(line + "\n"); frames -= 1; }
        if (end < 0) break;
        start = end + 1;
      }
    }
    const children: unknown[] = [];
    if (exception?.cause !== undefined) children.push(exception.cause);
    if (exception instanceof AggregateError) {
      for (const child of exception.errors) {
        if (children.length >= MAX_DESCENDANTS) { truncated = true; break; }
        children.push(child);
      }
    }
    const capacity = Math.max(0, MAX_DESCENDANTS + 1 - seen.size - pending.length);
    if (current.depth >= MAX_DEPTH) truncated ||= children.length > 0;
    else {
      truncated ||= children.length > capacity;
      for (let i = Math.min(children.length, capacity) - 1; i >= 0; i -= 1) {
        pending.push({ error: children[i], depth: current.depth + 1, label: "原因: " });
      }
    }
  }
  if (pending.length) truncated = true;
  if (truncated) parts.push(clip(MARKER, budget));
  return parts.join("");
}
